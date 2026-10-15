"""The functions and operators of formulas, described for users: what they do, how they are
written, an example. The curated list covers what tables are usually built with; the other
functions DuckDB offers follow, with DuckDB's own descriptions where it has them."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import cache

import duckdb

CURSOR = "‸"  # where the cursor goes after inserting a function

CONDITIONS = "Conditions"
OPERATORS = "Operators"
TEXT = "Text"
NUMBERS = "Numbers"
DATES = "Dates"
CONVERSION = "Conversion"
WINDOW = "Groups and order"
MORE = "More from DuckDB"
CATEGORIES = (CONDITIONS, OPERATORS, TEXT, NUMBERS, DATES, CONVERSION, WINDOW, MORE)


@dataclass(frozen=True)
class Function:
    """A function or operator: ``syntax`` shows how it is written, ``insert`` is what goes into a
    formula (``CURSOR`` marks where typing goes on; empty: derived from the syntax, or nothing).
    ``aliases`` are other names that work too; ``instead_of`` are names from SQL Server or
    spreadsheets that don't work in formulas, where this one does the job."""

    name: str
    category: str
    syntax: str
    summary: str
    example: str = ""  # a formula: tested, can be copied
    insert: str = ""
    helper: bool = False  # one of the SQL Server helpers Tafl adds (IIF, ISNULL, ...)
    note: str = ""
    aliases: tuple[str, ...] = ()
    instead_of: tuple[str, ...] = ()
    sql_example: str = ""  # DuckDB's own example, in plain SQL

    @property
    def template(self) -> str:
        return self.insert or (f"{self.name}({CURSOR})" if self.syntax.startswith(f"{self.name}(") else "")


def _f(name, category, syntax, summary, example="", insert="", helper=False, note="", aliases=(), instead_of=()):
    return Function(name, category, syntax, summary, example, insert, helper, note, tuple(aliases), tuple(instead_of))


CURATED: tuple[Function, ...] = (
    # Conditions
    _f("IIF", CONDITIONS, "IIF(condition, if_true, if_false)", "One of two values, depending on a condition (as in SQL Server)",
       "IIF([Amount] > 1000, 'big', 'small')", f"IIF({CURSOR}, , )", helper=True, instead_of=("IF",)),
    _f("CASE", CONDITIONS, "CASE WHEN condition THEN value [WHEN ...] ELSE value END", "The value of the first condition that is true",
       "CASE WHEN [Amount] > 1000 THEN 'big' WHEN [Amount] > 100 THEN 'medium' ELSE 'small' END", f"CASE WHEN {CURSOR} THEN  ELSE  END",
       note="Without ELSE, the result is empty when no condition is true."),
    _f("ISNULL", CONDITIONS, "ISNULL(value, fallback)", "The value, or the fallback when it is empty (as in SQL Server)",
       "ISNULL([Phone], '-')", f"ISNULL({CURSOR}, )", helper=True, aliases=("IFNULL",), instead_of=("NVL",)),
    _f("COALESCE", CONDITIONS, "COALESCE(value, other, ...)", "The first value that is not empty", "COALESCE([Phone], [Mobile], '-')"),
    _f("NULLIF", CONDITIONS, "NULLIF(value, other)", "Empty when the value equals the other, otherwise the value",
       "[Net] / NULLIF([Amount], 0)", note="Useful against division by zero: without it, dividing by 0 gives ∞ (infinity)."),
    _f("GREATEST", CONDITIONS, "GREATEST(value, other, ...)", "The largest of the values (empty values are skipped)",
       "GREATEST([Amount], 100)", instead_of=("MAX",)),
    _f("LEAST", CONDITIONS, "LEAST(value, other, ...)", "The smallest of the values (empty values are skipped)",
       "LEAST([Amount], 100)", instead_of=("MIN",)),
    # Operators
    _f("= <> < > <= >=", OPERATORS, "a = b, a <> b, a < b, ...", "Compare: equal, not equal (also !=), less than, greater than and so on",
       "[Country] <> 'DE'",
       note="Texts are compared case-sensitively: 'DE' = 'de' is false (unlike SQL Server's usual setting). "
       "Compare LOWER([Country]) = 'de', or use ILIKE."),
    _f("AND", OPERATORS, "condition AND condition", "True when both conditions are true", "[Amount] > 100 AND [Country] = 'DE'", " AND "),
    _f("OR", OPERATORS, "condition OR condition", "True when at least one of the conditions is true",
       "[Country] = 'DE' OR [Country] = 'AT'", " OR "),
    _f("NOT", OPERATORS, "NOT condition", "True when the condition is false", "NOT [Country] = 'DE'", "NOT "),
    _f("IS NULL", OPERATORS, "value IS NULL", "True when the value is empty", "[Phone] IS NULL", " IS NULL"),
    _f("IS NOT NULL", OPERATORS, "value IS NOT NULL", "True when the value is not empty", "[Phone] IS NOT NULL", " IS NOT NULL"),
    _f("IN", OPERATORS, "value IN (a, b, ...)", "True when the value is one of the listed values (NOT IN: none of them)",
       "[Country] IN ('DE', 'AT', 'CH')", f" IN ({CURSOR})"),
    _f("BETWEEN", OPERATORS, "value BETWEEN low AND high", "True when low <= value <= high", "[Amount] BETWEEN 100 AND 500",
       f" BETWEEN {CURSOR} AND "),
    _f("LIKE", OPERATORS, "text LIKE pattern", "True when the text fits the pattern: % any text, _ one character (case-sensitive)",
       "[Code] LIKE 'A-%'", f" LIKE '{CURSOR}'"),
    _f("ILIKE", OPERATORS, "text ILIKE pattern", "Like LIKE, but upper and lower case don't matter", "[Name] ILIKE 'a%'", f" ILIKE '{CURSOR}'"),
    _f("||", OPERATORS, "text || text", "Joins texts (+ only adds numbers)", "[First] || ' ' || [Last]", " || ",
       note="When one of the values is empty, the result is empty; CONCAT skips empty values."),
    _f("+ - * /", OPERATORS, "a + b, a - b, a * b, a / b", "Add, subtract, multiply, divide", "[Price] * [Amount]"),
    _f("//", OPERATORS, "a // b", "Whole-number division of whole numbers: 7 // 2 = 3", "[Amount] // 7", " // ",
       note="It rounds toward zero: -7 // 2 = -3. For decimal numbers // divides normally (7.5 // 2 = 3.75): "
       "use FLOOR(a / b) or TRUNC(a / b) there."),
    _f("%", OPERATORS, "a % b", "The remainder of a division: 7 % 2 = 1", "[Amount] % 7", " % "),
    # Text
    _f("UPPER", TEXT, "UPPER(text)", "The text in uppercase", "UPPER([Name])", aliases=("UCASE",), instead_of=("PROPER", "INITCAP"),
       note="A capital first letter only (like PROPER for one word): UPPER(LEFT([Name], 1)) || LOWER(SUBSTRING([Name], 2))."),
    _f("LOWER", TEXT, "LOWER(text)", "The text in lowercase", "LOWER([Name])", aliases=("LCASE",)),
    _f("TRIM", TEXT, "TRIM(text [, characters])", "Removes spaces (or the given characters) from both ends", "TRIM([Name])"),
    _f("LTRIM", TEXT, "LTRIM(text [, characters])", "Removes spaces (or the given characters) from the start", "LTRIM([Code], '0')"),
    _f("RTRIM", TEXT, "RTRIM(text [, characters])", "Removes spaces (or the given characters) from the end", "RTRIM([Name])"),
    _f("LEN", TEXT, "LEN(text)", "The number of characters; spaces at the end don't count (as in SQL Server)", "LEN([Name])",
       helper=True),
    _f("LENGTH", TEXT, "LENGTH(text)", "The number of characters, all spaces counted", "LENGTH([Name])"),
    _f("LEFT", TEXT, "LEFT(text, count)", "The first count characters", "LEFT([Zip], 2)"),
    _f("RIGHT", TEXT, "RIGHT(text, count)", "The last count characters", "RIGHT([Code], 3)"),
    _f("SUBSTRING", TEXT, "SUBSTRING(text, start [, length])", "Part of the text; start 1 is the first character",
       "SUBSTRING([Code], 2, 3)", aliases=("SUBSTR",), instead_of=("MID",)),
    _f("REPLACE", TEXT, "REPLACE(text, find, replacement)", "Replaces every occurrence of find (case-sensitive)",
       "REPLACE([Phone], ' ', '')", instead_of=("SUBSTITUTE",)),
    _f("CONCAT", TEXT, "CONCAT(value, value, ...)", "Joins the values; empty values are skipped", "CONCAT([First], ' ', [Last])"),
    _f("CONCAT_WS", TEXT, "CONCAT_WS(separator, value, value, ...)", "Joins the values with a separator; empty values are skipped",
       "CONCAT_WS(', ', [Street], [City])"),
    _f("CHARINDEX", TEXT, "CHARINDEX(find, text)",
       "Where find is in the text (1 = first character), 0 if it isn't; case-sensitive", "CHARINDEX('-', [Code])", helper=True,
       note="Unlike SQL Server's usual setting, upper and lower case matter: CHARINDEX(LOWER('B'), LOWER([Code])) ignores them.",
       instead_of=("FIND", "SEARCH")),
    _f("INSTR", TEXT, "INSTR(text, find)", "Where find is in the text (1 = first character), 0 if it isn't; case-sensitive",
       "INSTR([Code], '-')", aliases=("STRPOS", "POSITION")),
    _f("CONTAINS", TEXT, "CONTAINS(text, find)", "True when the text contains find (case-sensitive)", "CONTAINS([First], 'nn')"),
    _f("STARTS_WITH", TEXT, "STARTS_WITH(text, start)", "True when the text starts with the given text (case-sensitive)",
       "STARTS_WITH([Code], 'A')"),
    _f("ENDS_WITH", TEXT, "ENDS_WITH(text, end)", "True when the text ends with the given text (case-sensitive)", "ENDS_WITH([File], '.csv')"),
    _f("LPAD", TEXT, "LPAD(text, length, character)", "Fills up on the left to the length, e.g. with leading zeros",
       "LPAD(CAST([Amount] AS VARCHAR), 6, '0')",
       note="A longer text is cut to the length: LPAD('1234567', 3, '0') gives '123'. Numbers need CAST(... AS VARCHAR) first."),
    _f("RPAD", TEXT, "RPAD(text, length, character)", "Fills up on the right to the length", "RPAD([Code], 8, '_')",
       note="A longer text is cut to the length."),
    _f("SPLIT_PART", TEXT, "SPLIT_PART(text, separator, number)", "The n-th part of the text, split at the separator; -1 is the last part",
       "SPLIT_PART([Path], '\\', -1)", note="A part that doesn't exist gives an empty text ('')."),
    _f("REPEAT", TEXT, "REPEAT(text, count)", "The text repeated count times", "REPEAT('*', 3)", instead_of=("REPLICATE", "REPT")),
    _f("REVERSE", TEXT, "REVERSE(text)", "The characters in reverse order", "REVERSE([Code])"),
    _f("TRANSLATE", TEXT, "TRANSLATE(text, from, to)", "Replaces single characters: the n-th of from by the n-th of to",
       "TRANSLATE([Name], 'äöü', 'aou')"),
    _f("STRIP_ACCENTS", TEXT, "STRIP_ACCENTS(text)", "Removes accents and umlaut dots: 'Müller Ångström' gives 'Muller Angstrom'",
       "STRIP_ACCENTS([Name])"),
    _f("REGEXP_MATCHES", TEXT, "REGEXP_MATCHES(text, pattern)", "True when the regular expression matches somewhere in the text",
       "REGEXP_MATCHES([Code], '^[A-Z]-\\d+$')"),
    _f("REGEXP_EXTRACT", TEXT, "REGEXP_EXTRACT(text, pattern [, group])", "The part that matches the regular expression (or one group), '' if none",
       "REGEXP_EXTRACT([File], '\\d{4}-\\d{2}-\\d{2}')"),
    _f("REGEXP_REPLACE", TEXT, "REGEXP_REPLACE(text, pattern, replacement [, 'g'])", "Replaces the first match ('g': every match)",
       "REGEXP_REPLACE([Phone], '[^0-9]', '', 'g')"),
    _f("FORMAT", TEXT, "FORMAT(template, value, ...)", "Fills the {} in the template: {:.2f} two decimals, {:,} thousands separators",
       "FORMAT('{} ({:.1f})', [First], [Price])",
       note="{:.2f} needs a decimal number: for whole numbers write [Amount] * 1.0. When a value is empty, the result is empty."),
    _f("CHR", TEXT, "CHR(code)", "The character with this code; CHR(10) is a line break", "[First] || CHR(10) || [Country]",
       instead_of=("CHAR",)),
    _f("LEVENSHTEIN", TEXT, "LEVENSHTEIN(text, other)", "How many characters must change to turn one text into the other",
       "LEVENSHTEIN([First], 'Ana')", note="Useful to find similar spellings."),
    _f("JARO_WINKLER_SIMILARITY", TEXT, "JARO_WINKLER_SIMILARITY(text, other)", "How similar two texts are, from 0 (not at all) to 1 (equal)",
       "JARO_WINKLER_SIMILARITY([First], 'Ana')"),
    _f("MD5", TEXT, "MD5(text)", "A 32-character code calculated from the text, e.g. as a key", "MD5([Code])"),
    _f("GEN_RANDOM_UUID", TEXT, "GEN_RANDOM_UUID()", "A new random ID (UUID) for every row", "CAST(GEN_RANDOM_UUID() AS VARCHAR)",
       "GEN_RANDOM_UUID()", instead_of=("NEWID",)),
    # Numbers
    _f("ROUND", NUMBERS, "ROUND(number [, decimals])", "Rounds to the given number of decimals (halves away from zero)",
       "ROUND([Price] * 1.19, 2)",
       note="Decimal numbers are stored in binary, so a few halves such as 1.005 round down. For exact results "
       "round a DECIMAL: ROUND(CAST([Price] AS DECIMAL(18,3)), 2)."),
    _f("CEIL", NUMBERS, "CEIL(number)", "Rounds up to a whole number", "CEIL([Price])", aliases=("CEILING",),
       note="The result stays a decimal number (3.0); CAST(CEIL([Price]) AS BIGINT) gives an integer."),
    _f("FLOOR", NUMBERS, "FLOOR(number)", "Rounds down to a whole number", "FLOOR([Price])",
       note="The result stays a decimal number (3.0); CAST(FLOOR([Price]) AS BIGINT) gives an integer."),
    _f("TRUNC", NUMBERS, "TRUNC(number [, decimals])", "Cuts off the decimals (or keeps the given number of them)", "TRUNC([Price])",
       note="The result stays a decimal number (3.0); CAST(TRUNC([Price]) AS BIGINT) gives an integer."),
    _f("ABS", NUMBERS, "ABS(number)", "The number without its sign", "ABS([Amount] - 100)"),
    _f("SIGN", NUMBERS, "SIGN(number)", "The sign of the number: -1, 0 or 1", "SIGN([Amount] - 100)"),
    _f("MOD", NUMBERS, "MOD(number, divisor)", "The remainder of a division (the same as number % divisor)", "MOD([Amount], 7)"),
    _f("POWER", NUMBERS, "POWER(number, exponent)", "The number to the power of the exponent (also number ** exponent)", "POWER(1.05, 10)",
       aliases=("POW",)),
    _f("SQRT", NUMBERS, "SQRT(number)", "The square root", "SQRT([Amount])"),
    _f("EXP", NUMBERS, "EXP(number)", "e to the power of the number", "EXP(1)"),
    _f("LN", NUMBERS, "LN(number)", "The natural logarithm", "LN([Amount])"),
    _f("LOG", NUMBERS, "LOG(number) or LOG(base, number)", "The logarithm to base 10 (or to the given base)", "LOG(2, 1024)"),
    _f("RANDOM", NUMBERS, "RANDOM()", "A random number between 0 and 1, new for every row and calculation", "RANDOM()", "RANDOM()",
       instead_of=("RAND",)),
    _f("PI", NUMBERS, "PI()", "The number π (3.14159…)", "ROUND(PI(), 4)", "PI()"),
    # Dates
    _f("GETDATE", DATES, "GETDATE()", "The current date and time (as in SQL Server)", "GETDATE()", "GETDATE()", helper=True,
       instead_of=("NOW",)),
    _f("TODAY", DATES, "TODAY()", "The current date (also CURRENT_DATE)", "TODAY()", "TODAY()",
       note="For a date column, TODAY() - [Date] gives the days since the date; for date-times use DATE_DIFF('day', [Time], GETDATE()).",
       aliases=("CURRENT_DATE",)),
    _f("YEAR", DATES, "YEAR(date)", "The year", "YEAR([Date])"),
    _f("QUARTER", DATES, "QUARTER(date)", "The quarter, 1 to 4", "QUARTER([Date])"),
    _f("MONTH", DATES, "MONTH(date)", "The month, 1 to 12", "MONTH([Date])"),
    _f("WEEK", DATES, "WEEK(date)", "The ISO week number, 1 to 53", "WEEK([Date])",
       note="Days around New Year can belong to week 1 of the next year or week 53 of the previous one: "
       "use ISOYEAR([Date]) with it (YEARWEEK([Date]) gives 202501)."),
    _f("ISOYEAR", DATES, "ISOYEAR(date)", "The year that the ISO week belongs to (for 30 Dec 2024: 2025)", "ISOYEAR([Date])"),
    _f("DAY", DATES, "DAY(date)", "The day of the month, 1 to 31", "DAY([Date])"),
    _f("DAYOFYEAR", DATES, "DAYOFYEAR(date)", "The day of the year, 1 to 366", "DAYOFYEAR([Date])"),
    _f("ISODOW", DATES, "ISODOW(date)", "The weekday: 1 = Monday ... 7 = Sunday", "ISODOW([Date])", instead_of=("WEEKDAY",)),
    _f("DAYNAME", DATES, "DAYNAME(date)", "The English name of the weekday", "DAYNAME([Date])", instead_of=("DATENAME",)),
    _f("MONTHNAME", DATES, "MONTHNAME(date)", "The English name of the month", "MONTHNAME([Date])", instead_of=("DATENAME",)),
    _f("HOUR", DATES, "HOUR(date_time)", "The hour, 0 to 23", "HOUR(GETDATE())"),
    _f("MINUTE", DATES, "MINUTE(date_time)", "The minute, 0 to 59", "MINUTE(GETDATE())"),
    _f("SECOND", DATES, "SECOND(date_time)", "The second, 0 to 59", "SECOND(GETDATE())"),
    _f("DATE_TRUNC", DATES, "DATE_TRUNC('part', date)", "The start of the year, quarter, month, week or day",
       "DATE_TRUNC('month', [Date])", f"DATE_TRUNC('{CURSOR}', )", note="The result is a date-time; add ::DATE for a date.",
       aliases=("DATETRUNC",)),
    _f("DATE_PART", DATES, "DATE_PART('part', date)", "One part of a date, e.g. year, month, day, week, isodow, doy or hour",
       "DATE_PART('doy', [Date])", f"DATE_PART('{CURSOR}', )", aliases=("DATEPART",),
       note="The part is written in quotes, as the first argument."),
    _f("DATE_DIFF", DATES, "DATE_DIFF('part', start, end)", "How many day, month, year (...) boundaries lie between two dates",
       "DATE_DIFF('day', [Date], DATE '2025-12-31')", f"DATE_DIFF('{CURSOR}', , )", aliases=("DATEDIFF",),
       note="It counts the boundaries crossed, as DATEDIFF in SQL Server: 31 January to 1 February is 1 month, "
       "31 December to 1 January is 1 year. For complete months or years, e.g. an age, use DATE_SUB. "
       "The part is written in quotes: 'day', 'month', 'year', 'hour', 'minute'."),
    _f("DATE_SUB", DATES, "DATE_SUB('part', start, end)", "How many complete days, months or years (...) lie between two dates",
       "DATE_SUB('year', [Date], TODAY())", f"DATE_SUB('{CURSOR}', , )", aliases=("DATESUB",),
       note="31 January to 1 February is 0 months. DATE_SUB('year', [Birthday], TODAY()) gives the age."),
    _f("+ INTERVAL", DATES, "date + INTERVAL n DAY|MONTH|YEAR", "The date plus days, months or years",
       "[Date] + INTERVAL 1 MONTH", f" + INTERVAL {CURSOR} DAY", instead_of=("DATEADD", "EDATE"),
       note="The result is a date-time; add ::DATE for a date. With a column write INTERVAL ([Months]) MONTH, or "
       "[Date] + TO_MONTHS([Months]). [Date] + 30 adds 30 days and stays a date."),
    _f("LAST_DAY", DATES, "LAST_DAY(date)", "The last day of the month", "LAST_DAY([Date])", instead_of=("EOMONTH",)),
    _f("MAKE_DATE", DATES, "MAKE_DATE(year, month, day)", "A date from its parts", "MAKE_DATE(YEAR([Date]), 1, 1)",
       instead_of=("DATEFROMPARTS",)),
    _f("STRFTIME", DATES, "STRFTIME(date, format)", "The date as text, e.g. %d.%m.%Y, %Y-%m, %B (month name) or %H:%M",
       "STRFTIME([Date], '%d.%m.%Y')", instead_of=("TEXT",)),
    _f("STRPTIME", DATES, "STRPTIME(text, format)", "Reads a date-time from text in the given format; an error if it doesn't fit",
       "STRPTIME('31.12.2025', '%d.%m.%Y')", note="The result is a date-time; add ::DATE for a date."),
    _f("TRY_STRPTIME", DATES, "TRY_STRPTIME(text, format)", "Like STRPTIME, but empty when the text doesn't fit",
       "TRY_STRPTIME('31.12.2025', '%d.%m.%Y')", note="The result is a date-time; add ::DATE for a date."),
    # Conversion
    _f("CAST", CONVERSION, "CAST(value AS type)",
       "Converts to VARCHAR (text), BIGINT (whole number), DOUBLE (decimal number), DECIMAL(18,2), DATE, TIMESTAMP (date-time) "
       "or BOOLEAN (true/false)", "CAST([Zip] AS BIGINT)", f"CAST({CURSOR} AS )", instead_of=("CONVERT",),
       note="BIGINT rounds to the nearest whole number (halves to the even one): use TRUNC or FLOOR first to cut off. "
       "Text must be written 2025-12-31 and 1.5: use STRPTIME for other date formats and REPLACE([Text], ',', '.') for a decimal comma."),
    _f("TRY_CAST", CONVERSION, "TRY_CAST(value AS type)", "Like CAST, but empty instead of an error when it can't be converted",
       "TRY_CAST([Code] AS BIGINT)", f"TRY_CAST({CURSOR} AS )", instead_of=("TRY_CONVERT", "ISNUMERIC", "ISDATE"),
       note="TRY_CAST([Value] AS DOUBLE) IS NOT NULL tells whether a text is a number."),
    _f("::", CONVERSION, "value::type", "The short form of CAST", "[Amount]::VARCHAR", "::"),
    _f("DATE '...'", CONVERSION, "DATE 'yyyy-mm-dd'", "A fixed date (also TIMESTAMP '2025-12-31 23:59')", "[Date] >= DATE '2025-01-01'",
       f"DATE '{CURSOR}'"),
    # Groups and order (window functions)
    _f("SUM ... OVER", WINDOW, "SUM(value) OVER (PARTITION BY column)", "The total of the row's group on every row (OVER (): the whole table)",
       "[Amount] / SUM([Amount]) OVER (PARTITION BY [Customer])", f"SUM({CURSOR}) OVER (PARTITION BY )",
       note="AVG, MIN, MAX and COUNT work the same way.", instead_of=("SUMIF",)),
    _f("COUNT(*) ... OVER", WINDOW, "COUNT(*) OVER (PARTITION BY column)", "The number of rows in the row's group",
       "COUNT(*) OVER (PARTITION BY [Customer])", f"COUNT(*) OVER (PARTITION BY {CURSOR})", instead_of=("COUNTIF",)),
    _f("Running total", WINDOW, "SUM(value) OVER (ORDER BY column)", "The running total in the given order (per group with PARTITION BY)",
       "SUM([Amount]) OVER (PARTITION BY [Customer] ORDER BY [Date])", f"SUM({CURSOR}) OVER (ORDER BY )",
       note="Rows with the same value in the ORDER BY column get the same total; add a second column to ORDER BY, "
       "or ROWS UNBOUNDED PRECEDING, to add them one by one."),
    _f("ROW_NUMBER", WINDOW, "ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)", "Numbers the rows 1, 2, 3 ... within each group",
       "ROW_NUMBER() OVER (PARTITION BY [Customer] ORDER BY [Date])", f"ROW_NUMBER() OVER (ORDER BY {CURSOR})"),
    _f("RANK", WINDOW, "RANK() OVER (ORDER BY ...)", "The rank; equal values share a rank, then numbers are skipped",
       "RANK() OVER (ORDER BY [Amount] DESC)", f"RANK() OVER (ORDER BY {CURSOR})"),
    _f("DENSE_RANK", WINDOW, "DENSE_RANK() OVER (ORDER BY ...)", "The rank without gaps after equal values",
       "DENSE_RANK() OVER (ORDER BY [Amount] DESC)", f"DENSE_RANK() OVER (ORDER BY {CURSOR})"),
    _f("LAG", WINDOW, "LAG(value [, rows]) OVER (ORDER BY ...)", "The value of the previous row (or rows back)",
       "[Amount] - LAG([Amount]) OVER (PARTITION BY [Customer] ORDER BY [Date])", f"LAG({CURSOR}) OVER (ORDER BY )"),
    _f("LEAD", WINDOW, "LEAD(value [, rows]) OVER (ORDER BY ...)", "The value of the next row (or rows ahead)",
       "LEAD([Date]) OVER (PARTITION BY [Customer] ORDER BY [Date])", f"LEAD({CURSOR}) OVER (ORDER BY )"),
    _f("FIRST_VALUE", WINDOW, "FIRST_VALUE(value) OVER (PARTITION BY ... ORDER BY ...)", "The first value in the row's group",
       "FIRST_VALUE([Amount]) OVER (PARTITION BY [Customer] ORDER BY [Date])", f"FIRST_VALUE({CURSOR}) OVER (PARTITION BY  ORDER BY )"),
    _f("LAST_VALUE", WINDOW, "LAST_VALUE(value) OVER (PARTITION BY ... ORDER BY ... ROWS BETWEEN ...)", "The last value in the row's group",
       "LAST_VALUE([Amount]) OVER (PARTITION BY [Customer] ORDER BY [Date] ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)",
       f"LAST_VALUE({CURSOR}) OVER (PARTITION BY  ORDER BY  ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)",
       note="Without ROWS BETWEEN ..., the window ends at the current row (and rows with the same order value)."),
    _f("NTILE", WINDOW, "NTILE(n) OVER (ORDER BY ...)", "Splits the rows into n groups of nearly equal size: 1 ... n",
       "NTILE(4) OVER (ORDER BY [Amount])", f"NTILE({CURSOR}) OVER (ORDER BY )"),
    _f("STRING_AGG ... OVER", WINDOW, "STRING_AGG(text, separator) OVER (PARTITION BY ...)", "All texts of the row's group, joined",
       "STRING_AGG([Code], ', ') OVER (PARTITION BY [Customer])", f"STRING_AGG({CURSOR}, ', ') OVER (PARTITION BY )",
       instead_of=("TEXTJOIN",)),
)  # fmt: skip

# DuckDB functions that are internal, need things a formula can't have, or break the result.
_HIDDEN = {
    "sum_no_overflow", "current_query", "current_query_id", "current_connection_id", "current_transaction_id",
    "txid_current", "nextval", "currval", "sleep_ms", "setseed", "write_log", "stats", "alias", "vector_type",
    "constant_or_null", "unpivot_list", "create_sort_key", "parse_duckdb_log_message", "in_search_path",
    "current_schema", "current_schemas", "current_database", "current_catalog", "current_setting",
    "current_user", "current_role", "session_user", "user", "version", "can_cast_implicitly", "get_type",
    "make_type", "replace_type", "cast_to_type", "remap_struct", "timetz_byte_comparable", "is_histogram_other_bin",
    "row", "get_current_time", "get_current_timestamp", "transaction_timestamp", "error", "finalize", "combine",
    "add_parquet_key", "parquet_file_metadata", "sql_auto_complete", "which_secret", "checkpoint",
    "regexp_split_to_table", "get_block_size", "getvariable",
}  # fmt: skip
_HIDDEN_PREFIXES = ("union_", "enum_", "variant_", "st_", "icu_", "pg_", "__", "duckdb_", "has_", "json_", "parquet_")


@cache
def more_functions() -> tuple[Function, ...]:
    """DuckDB's other functions, named in capitals, with DuckDB's description where it has one."""
    known = set()
    for function in CURATED:
        known.add(re.split(r"[ (.]", function.name)[0].lower())
        known.update(alias.lower() for alias in function.aliases)
    con = duckdb.connect()
    try:
        rows = con.sql(
            "SELECT function_name, function_type, parameters, description, examples, alias_of "
            "FROM duckdb_functions() WHERE schema_name = 'main' AND function_type IN ('scalar', 'aggregate', 'macro')"
        ).fetchall()
    finally:
        con.close()
    overloads: dict[str, list[tuple]] = {}
    for row in rows:
        overloads.setdefault(row[0], []).append(row)
    found = []
    for name in sorted(overloads):
        if name in known or name in _HIDDEN or name.startswith(_HIDDEN_PREFIXES) or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            continue
        variants = overloads[name]
        if any((v[5] or "") in known for v in variants):  # another name for a curated function
            continue
        descriptions = [
            v[3].strip() for v in variants if v[3] and v[3].strip() and not v[3].strip().lower().startswith(name)
        ]
        if any(d.lower().startswith("internal") for d in descriptions):
            continue
        description = Counter(descriptions).most_common(1)[0][0].replace("`", "") if descriptions else ""
        parameters = [_parameter(p) for p in max((v[2] or [] for v in variants), key=len)]
        sentences = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)
        summary = sentences[0].strip().rstrip(".").strip() or "No description from DuckDB"
        note = sentences[1].strip() if len(sentences) > 1 else ""
        if any(v[1] == "aggregate" for v in variants):
            note = (note + "\n\n" if note else "") + "In a formula it needs OVER (...), e.g. OVER (PARTITION BY [Customer])."
        examples = [e for v in variants for e in (v[4] or []) if re.search(rf"\b{name}\s*\(", e, re.IGNORECASE)]
        upper = name.upper()
        found.append(
            Function(upper, MORE, f"{upper}({', '.join(parameters)})", summary, note=note, sql_example=examples[0] if examples else "")
        )
    return tuple(found)


def _parameter(name: str) -> str:
    name = name.strip("[]")
    return "value" if re.fullmatch(r"col\d+", name) else name


def all_functions() -> list[Function]:
    """The curated functions, then the other functions DuckDB offers."""
    return [*CURATED, *more_functions()]


def search(text: str, category: str | None = None) -> list[Function]:
    """The functions that match every word of ``text`` (names, syntax, description), best first:
    the name itself (or a name it is used instead of), then the curated functions before
    DuckDB's others, each with names that start with the word first."""
    words = text.lower().split()
    candidates = [f for f in all_functions() if category in (None, f.category)]
    if not words:
        return candidates
    scored = []
    for order, function in enumerate(candidates):
        names = [function.name.lower(), *(n.lower() for n in function.aliases + function.instead_of)]
        described = " ".join([*names, function.syntax, function.summary]).lower()
        haystack = f"{described} {function.note.lower()} {function.category.lower()}"
        if not all(word in haystack for word in words):
            continue
        first = words[0]
        if first in names:
            rank = 0
        elif any(n.startswith(first) for n in names):
            rank = 1
        elif any(first in n for n in names):
            rank = 2
        else:  # in the description, else only in a note
            rank = 3 if first in described else 4
        scored.append((rank > 0, function.category == MORE, rank, order, function))
    return [function for *_, function in sorted(scored, key=lambda s: s[:4])]

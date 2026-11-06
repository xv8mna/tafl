# Changes

## 0.6.4 – unreleased

This version fixes wrong results without a message. Steps that treat numbers, dates and times as
text now use the same text on every PC, as **Pivot** writes it. Columns of different types get a
type that holds every value. Formulas, filters, imports and exports keep unsigned and very long
whole numbers and fractions of a second. Table statuses stay right after more kinds of edits. Many
technical errors are now plain messages, and some steps on tables with thousands of columns are
much faster. Some results of stored projects change: **Results that change** lists them first.

### Results that change

Where a project of 0.6.3 meets one of these changes, it gives other results now, or a step stops
with a message that says what to change.

- **Aggregate**: a *Sum* of only empty values is empty, as in SQL and in **Pivot**: in a group
  whose values are all empty, and for a table without rows. Before, it was 0. *Sum*, *Average* and
  *Median* of a column of the type *empty* are empty too. Before, they stopped the step, for
  example with "Sum needs a number column; Sales is a empty column".
- **Merge**: keys of different types, such as a number and a text, are compared as text that is
  the same on every PC, as **Pivot** writes values. The decimal number 17.0 is `17`, 1e16 is
  `10000000000000000` and -0.0 is `0`. Before, Tafl compared `17.0`, `1e+16` and `-0.0`.
- **Merge**: the decimal number 17.0 now finds the text `17`, and a text key `17.0` or `2.0` no
  longer finds the number 17.0 or 2.0. To match such texts, first make the text column a decimal
  number with **Change type**: then the keys are compared as numbers.
- **Merge**: a date-time key is compared as `2025-12-31 08:30:00`, with fractions of a second only
  when it has some, and with a time zone as its local time. Before, Tafl compared
  `2025-12-31 08:30:00.000000` and `2025-12-31 08:30:00.000000+01:00`. Keys in that form, for
  example from an earlier **Join texts**, find no match now. The step still warns that the keys
  are compared as text.
- **Merge**, *Full join* with keys compared as text: the added rows of the other table get the
  type of the key column when their key is such a text. Otherwise the key column becomes text, as
  before. A local time that the clocks skip or repeat in the column's zone counts as no date-time
  there.
- **Merge**: a time key against a date or date-time key, and a time-span key against a key of
  another type, stop the step with "… can't be compared: choose other key columns, …". This holds
  also when the key column has no values. Before, a lookup with such a column without values found
  no match. A *Full join* stopped with a technical error, or compared a time span with a text key
  as text.
- **Aggregate**, *Join texts*: values that are not text are joined as **Pivot** writes them: `2`,
  `10000000000000000`, `0`, `08:30:00.500`, `2025-12-31 08:30:00`, a time zone's local time.
  Before, Tafl wrote `2.0`, `1e+16`, `-0.0`, `2025-12-31 08:30:00.000000` and
  `2025-12-31 08:30:00.000000+01:00`. A time lost its fractions of a second (`08:30:00`), so two
  different times gave one text.
- **Clean text** and **Replace**: a column that is not text becomes text, as before, but now as
  **Pivot** writes values: `17`, `10000000000000000`, `2025-12-31 08:30:00` (a zone's local time),
  `08:30:00.500`, `1d 2h 3m`. Binary values become their text, or hex digits (`0xFF00`) when some
  are no text. Before, the text was `17.0`, `1E+16` (after *UPPER CASE*),
  `2025-12-31 08:30:00.000000+01:00`, and `08:30:00` for 08:30:00.5.
- **Filter rows**: *contains*, *does not contain*, *starts with* and *ends with* look at a text of
  each value that is the same on every PC, as **Pivot** writes it. That is `17` for the decimal
  number 17, `2025-12-31 08:30:00` for a date-time (the local time of its zone), `08:30:00.500`
  for a time and `1d 2h 3m` for a time span. So *contains* `31.12.` finds no date (that is the
  grid's format on this PC), but `-12-31` does.
- **Filter rows**, these operators in 0.6.3: they looked at `17.0`, `1e+16`, `-0.0`,
  `2025-12-31 08:30:00.000000` (with `+01:00` in a zone) and `08:30:00` for 08:30:00.5. So *ends
  with* `08:30:00` found no date-time, and *contains* `+01` found every winter time of Berlin. A
  column of time spans stopped the step.
- **Change type** to *Text*: date-times without a format keep the fractions of a second of a value
  that has some, with 3, 6 or 9 digits (`31.12.2025 08:30:00.500`); times keep theirs too. Values
  without fractions stay `31.12.2025 08:30:00`. Before, the fractions were left out, so 08:30:00.5
  and 08:30:00 gave one text. A format that you type is used as it is.
- **Replace**, *Regular expression*: in **Replace with**, a group number ends where its digits
  end: `$1_$2` is group 1, an underscore and group 2. Before, `$1_` was read as a group named
  "1_", so `PA1004` with `^([A-Z]+)([0-9]+)$` became `1004`. `${1}`, `${name}` and `$$` (a `$`
  sign) work as before.
- **Replace**, *Regular expression*: a group that the expression doesn't have, such as `$3` or
  `$name`, stops the step with a message that names it, also in steps made with 0.6.3. Before, it
  gave empty text, without a message. *Whole cell value* and *Text inside the value* read no
  groups.
- **Replace**, *Regular expression*, a backslash before a digit in **Replace with**: `\1`, `\2`, …
  are groups, as `$1`, `$2`, … are, and a backslash before a digit is written as two (`\\2026`
  gives `\2026`). Before, a backslash before a digit stayed as it was. So `D:\Archiv\2026\$1` now
  stops the step: the message says that there is no group 2026, and to write two backslashes.
  Other backslashes (`C:\data\$1`) work as before.
- **Append other tables**: a column with different types in the tables gets the type that
  **Unpivot** gives its value column, as long as every value stays as it is. Whole and decimal
  numbers become decimal numbers, whole numbers and exact decimals exact decimals (up to 38
  digits), and dates and date-times date-times. A table in which the column has only empty values
  doesn't count.
- **Append other tables**: any other mix of types becomes text, the same on every PC, as
  **Unpivot** writes it (`2025-01-02`, `true`, `1d 2h`). The step warns: "“Amount” is text: it has
  different types in the tables (integer, text)". Binary values whose bytes are all text become
  that text; one value that is no text makes all of them hex digits (`0x616263`). Lists and
  records beside other values stop the step.
- **Append other tables** in 0.6.3: 52 of the 105 pairs of 14 types and *empty* stopped the step
  with a technical error, such as a date beside true/false ("failed to determine supertype"). Most
  mixes with text became text without a word.
- **Append other tables** in 0.6.3 changed values without a message. A date beside a whole number
  became a number (2025-01-02 was 20090), date-times, times and time spans counts of microseconds
  or nanoseconds, and true 1. Text beside binary values became binary, and exact decimals beside
  decimal numbers decimal numbers. Whole numbers beside unsigned 64-bit numbers became 128-bit
  numbers, which only a plugin can use.
- **Import CSV / Excel / Parquet files** of a folder, and several sheets of a workbook: a column
  with different types in the files (or sheets) follows the rule of **Append**. Text gets a
  warning, such as "“Nr” is text: it has different types in the sheets (number, text)". Lists and
  records beside other values stop the import. CSV files are read as text and detected together,
  so their columns never mix types.
- **Import Excel files** and **Import Parquet files** of several files or sheets in 0.6.3: a date
  beside a whole number in Parquet files became a number (2025-01-02 was 20090), and an exact
  decimal beside a decimal number a decimal number. A number beside text in Excel sheets became
  text such as `5.0`. All this happened without a message. A date beside true/false stopped the
  import with "The files don't fit together: failed to determine supertype of date and bool".
- **Import Excel files** with several sheets: **Whole numbers as integers** and **Date-times at
  midnight as dates** convert each sheet on its own, before the sheets are put together. So a
  sheet of dates beside a sheet of numbers gives `2025-01-31` as text, with the warning. Before,
  such a date became the number 1738281600000, without a message.
- **Import Parquet files**: categories with a name of their own, as Polars 2.0 can write them,
  become text, as other categories do. Before, they stayed categories.
- **Unpivot**: a column whose values are all empty no longer decides the type of the value column,
  whatever its type: an empty text column, as a CSV import gives it, beside whole numbers leaves
  them whole numbers. With **Leave out empty values**, text with nothing but spaces doesn't count
  either. Before, all values became text; only a column of the type *empty* didn't count.
- **Unpivot**: whole numbers of different types beyond 9,223,372,036,854,775,807 (unsigned 64-bit
  numbers of a Parquet file beside ordinary ones) become exact decimals; only numbers of more than
  38 digits become text. Before, they became text.
- **Unpivot**: columns of categories beside text are text, without a message. Before, the step said
  "the columns have different types (categorical: a; text: b)".
- **Formulas** on unsigned whole numbers (see below): bit operators give the results of signed
  numbers. `~[Flags]` of 5 is -6 (0.6.3: 250 for an 8-bit column), and `[Flags] << 1` of 200 is
  400 (0.6.3 stopped with "Overflow in left shift (200 << 1)"). For 0.6.3's result of `~`, write
  `~[Flags] & 255`.
- **Export to CSV**: times keep their fractions of a second, in the text that is the same on every
  PC (`08:30:00.500`; whole seconds stay `08:30:00`). Before, 08:30:00.5 was written as
  `08:30:00`, whatever the date-time format.
- **Export to database** (SQLite) keeps fractions of a second: a time 08:30:00.5 is stored as
  `08:30:00.500000`, a date-time with nanoseconds with nine digits. Whole seconds and microseconds
  are written as before, so that keys still find the rows of earlier exports. Before, times lost
  their fractions (`08:30:00`) and date-times their nanoseconds. Different values became one, also
  as keys: an **Update** gave all rows of the same second the values of one.
- **Export to database** (SQL Server): binary values that go into a column of text (`char`,
  `varchar`, `nchar`, `nvarchar`, `text`, `ntext`) are written as hex text, such as `0x6162`, in
  every mode, as the CSV export writes them. Binary keys find the rows that hold that text. Hex
  text too long for its column stops the export with a message that names the column.
- **Export to database** (SQL Server), binary values for a column of text in 0.6.3: the bytes were
  read as characters without a message. `ab` became `ab` in a `varchar` column and `扡` in an
  `nvarchar` or `ntext` one. **Update** and **Upsert** wrote `?` into `varchar` columns, and `text`
  and `ntext` columns stopped the export with a technical error or a message about memory.
- **Tables that are off**: when tables use each other in a circle and one of them uses a table
  that is off, or skipped, every table of the circle is *skipped*, whichever table you select
  first. So `tafl run` ends with exit code 0, whichever tables you run. A circle without such a
  table is still an error.
- **Tables that are off** in a circle in 0.6.3: a table of the circle was *skipped* or had the
  error "Circular reference: B → A → B", by the table calculated first. `tafl run` gave the error
  (exit code 1), and `tafl run --table B` skipped it.
- **Circular reference** through a step after **Skip the rest if a table is empty**: table A uses B
  in such a step, and B starts from A. On a day without rows, A skips its rest and is ok, and B
  has the error "Circular reference: B → A → B", whichever table you select first. `tafl run` ends
  with exit code 1, also with `--table A` or `--table B`. Before, B was ok or had the error, by the
  table selected first, and `tafl run` and `tafl run --table A` ended with 0.
- Some stored steps now stop with a plain message where 0.6.3 gave a result. These are
  **Change type** to *Date* with `dmyyyy`, `dmyy` or `yyyymd`, and **Change type** of dates to
  *Text* with a number format such as `0.000`, `00000` or `#,##0`. So are **Filter rows** with a
  value on a column of lists or records, and **Fill empty values** with a fixed value on a column
  of records. Their bullets are below.

### A new setting

- **Import from database**: **Detect numbers and dates in text columns** has a **Decimal
  separator**, shown when detection is on. *Detect automatically*, the choice of a new step, reads
  numbers as the CSV import does. A column whose values show a decimal comma (`123,40`) has one,
  and the step names it ("Decimal comma in Price"). Before, `123,40` stayed text: only a decimal
  point was read.
- **Import from database**, **Decimal separator** *Detect automatically*: amounts (`1.234,50 €`) and
  percentages (`12,5%` is 0.125) become numbers, and whole numbers with thousands separators
  (`1,000`) whole numbers. A column whose values fit both (`1.000`) keeps the decimal point.
- **Import from database**, **Decimal separator**: *Point* and *Comma* read only numbers and dates,
  with that separator. A step saved by an earlier version gets *Point*, which reads as before. So a
  project that turned such texts into numbers with its own steps (**Replace**, **Change type**)
  gives the same results.

### Wrong results that are now right

- **Change type** to *Date* or *Date and time*: with a format of digits only, such as `yyyymmdd`,
  `ddmmyyyy`, `yymmdd` or `yyyymmddhhmmss`, a whole number is read as its digits. So 20260131 with
  `yyyymmdd` is 31 January 2026, and 1012026 with `ddmmyyyy` is 1 January 2026: the lost leading
  zero comes back. Whole decimal numbers, as an Excel import gives them, work the same way.
- **Change type** to *Date*, numbers with a format of digits only: a number with decimal places
  becomes empty with the usual warning. Before, the format was ignored: 20260131 became a day in
  the year 57370, and 1012026 the 30 October 4670, without a message. Without a format, or with
  separators such as `dd.mm.yyyy`, numbers are Excel day numbers, as before.
- **Change type** to *Date*: a text read with a format of digits only needs exactly its digits.
  One with fewer or more becomes empty with the usual warning. Before, the text `1012026` with
  `ddmmyyyy` became 10.12.0026, and `10126` with `ddmmyy` 10.12.2006, without a warning.
- **Change type** to *Date*: a format of digits only needs `dd` and `mm`. So `dmyyyy`, `dmyy` or
  `yyyymd` stop the step with "The date format “dmyyyy” has no separators, so each part needs all
  its digits: use dd and mm, not d and m (…), e.g. ddmmyyyy". Before, the number 1012026 with
  `dmyyyy` became 30.10.4670, and the text `1012026` 10.12.0026, without a message.
- **Change type** to *Date*: a number whose Excel day is not in the years 1 to 9999 (above
  2958465, which is 31.12.9999), NaN and infinity become empty with the usual warning. For such
  numbers it adds: "Numbers are Excel day numbers, up to 2,958,465 (31.12.9999): for a number such
  as 20260131, give the format yyyymmdd". With *stop with an error*, the step stops with that
  message.
- **Change type** to *Date* in 0.6.3: 20250101 became a day in the year 57342 without a warning,
  and an export to Excel then stopped. Very big numbers, NaN and infinity stopped the step with
  "conversion from f64 to i64 failed", also with *become empty*.
- **Change type** to *Whole number* says how many values it rounded, with up to three of them:
  "Amount: 2 value(s) were rounded, e.g. '2.5', '-0.5': a whole number has no decimal places
  (Decimal number and Text keep them)". Values are rounded half away from zero, as before (the
  text `1.000,5` becomes 1001). Before, there was no warning.
- **Formulas** on unsigned whole numbers (Parquet files of other programs can have them, for
  example unsigned 64-bit IDs): `-[Id]` is -1 for the Id 1, and `-[Id] < 0` keeps those rows.
  Before, it was 18446744073709551615, and the condition kept no row, without a message.
- **Formulas** on unsigned whole numbers are now calculated as signed ones, so `[Id] + 1` on
  18,446,744,073,709,551,615 and `0 - [Id]` give the right number. Before, they stopped with
  "Overflow in addition of UINT64" and "Overflow in subtraction of UINT64".
- **Formulas** on unsigned 128-bit whole numbers (only a plugin makes them): up to
  170,141,183,460,469,231,731,687,303,715,884,105,727 they are calculated as signed numbers. So
  `[v] - [w]` with a signed 64-bit number keeps all its digits, `-[v]` is negative, and
  `SUM([v]) OVER ()` is a whole number. Before, the difference gave a rounded decimal number
  (-9.223372036854776e+18), and the sum a decimal number (72.0). `-[v]` stopped with "the whole
  number 340,282,366,920,938,463,463,374,607,431,768,211,384 is too big".
- **Formulas** on a column of unsigned 128-bit whole numbers with a bigger number stop with a plain
  message that names the column and its highest number. This is so for a formula that mixes the
  column with signed numbers, compares it with another column, or takes `-x` or `a - x` of it (also
  `subtract(a, x)`). Before, such a formula gave a rounded decimal number, a wrong comparison
  (2^127 + 5 = 2^127 - 1 was true) or a wrong number (`-[v]` gave 2^128 - v), without a message.
- **Formulas**: `SUM([v]) OVER ()` of exact decimals stops with the plain message "the result needs
  more than 38 digits …" when the sum needs more digits, also with only the decimal places that the
  values use. Before, it gave a text of 39 digits (120000000000000000000000000000000000000),
  without a message.
- **Formulas**: a window sum of exact decimals that fits only with the decimal places that the
  values use is a decimal number (about 15 digits), as in 0.6.3. Whole numbers of a plugin's
  128-bit columns still keep a sum of 39 digits as text.
- **Pivot** and **Aggregate**: a sum of whole numbers of 128 bits (only a plugin makes them) beyond
  their range stops the step. The message is the one that other whole numbers give: "… too big for
  a whole number (at most 170,141,183,460,469,231,731,687,303,715,884,105,727)". Before, the sum
  wrapped around without a message: 2^127 − 1 + 5 gave
  −170141183460469231731687303715884105724, and 2^128 − 1 + 2 gave 1.
- **Instant filters**, **Add as step** on a column with a time zone: a time period (year, quarter,
  month, week, date) is taken in the local time of the column's zone, as the card groups it.
  Before, the step used this PC's time zone. So 2025-01-31 23:30 UTC was February in Berlin, and
  the January of a UTC column kept other rows.
- **Instant filters**, **Add as step** of a value of a column with a time zone keeps its row.
  Before, the step kept no row, because it read `TIMESTAMP '2025-01-31 23:30:00+00:00'` without
  its offset. With the PC in the column's zone, it kept the other row of a time that a clock
  change repeats.
- **Instant filters** on a column with a time zone: a picked value keeps its moment in every zone,
  also in **Show only rows where …** of the grid. Filters saved before keep their rows.
- **Instant filters** in 0.6.3, where the time-zone rules of Python and of Polars differ (for
  example America/Vancouver, Edmonton and Winnipeg from 2026-11-01, Africa/Casablanca from
  2026-09-20, Europe/Chisinau since 2022): a picked value was stored as another moment. Picking
  30.11.2026 23:30 in Vancouver kept the row of 22:30, or no row, and **Add as step** made a step
  that kept no row.
- **Instant filters**, **Add as step** of a value with nanoseconds, such as 08:30:00.000000001, of
  a date-time or a time of day keeps its row, and so does **Show only rows where …** in the grid.
  Before, 08:30:00 and 08:30:00.000000001 were two values in the card but stored as one. So the
  card and the step kept the row of 08:30:00.
- **Instant filters**, **Add as step** of a time span keeps its rows. Before, 1 µs made a step that
  stopped with "extract specifier "e" not recognized". 1 ns and 2 ns were both stored as 0 s, so
  the card kept no row and the step both. 300 years and 1 µs lost the µs, and the card kept no row.
- **Instant filters**, **Add as step** of a pattern: `\w`, `\d` and `\s` are the letters, digits
  and spaces of all languages, as in the card. Before, the step took those of ASCII only: `^\w+$`
  kept Straße and ä in the card only, and `\d` the Arabic-Indic digits ١٢٣.
- **Instant filters**, **Add as step** of a pattern: a repetition with spaces, such as `^a{1, 2}$`,
  is one in the step too; before, the step read it as the text "a{1, 2}". A named group
  `(?<name>…)` and a character written `\u{E4}` or `\u00E4` make steps too; before, these steps
  stopped with "invalid perl operator" and "invalid escape sequence".
- **Instant filters**: where a step can't keep the card's rows, the card says why ("Not added as a
  step: …") and no step is made. This is so for a pattern with `\b` or `\B`, with a flag other
  than i, m, s and U such as `(?x)`, or with a repetition of a repetition such as `a{2}{2}`.
  Before, such steps stopped with "invalid perl operator" or "bad repetition operator".
- **Instant filters**: time spans, or date-times with a time zone, that differ by less than a
  microsecond make no step either, as a step compares these to the microsecond.
- **Instant filters**: where a step could keep other rows than the card, **Add as step** first runs
  it on the data at the selected step. This is so for patterns, time periods, date-times with a
  time zone or with nanoseconds, times of day and time spans, up to 1,000 picked values. Where the
  rows differ, the card says "a step would keep other rows than the filter: 1 of the 2 rows here";
  where the step would stop on the data, it says that.
- **Instant filters** in 0.6.3 made such steps: the quarter of 1969-12-31 23:59:59.999999999 was
  1969 Q4 in the card and 1970 Q1 in the step. `^\w+$` in the step didn't match the letters of
  Unicode 15.1 and 16, such as U+13460.
- **Instant filters**: **Add as step** also runs the step first, for any number of values, where a
  stored value isn't a value of the column's type. Only a project file can hold one, such as a
  number on a column of categories or a whole number beyond the column's type. A text with the
  character U+0000 or a broken character is refused. Before, such steps stopped at every run or
  kept other rows. Filters that 0.6.3 saved keep their rows.
- **Filter rows**: a typed date-time with more digits than its column holds keeps them and is
  compared exactly. In a column of milliseconds (Parquet files and Excel imports can have them),
  `2025-01-01 08:30:00.0005` equals no value and is between 08:30:00 and 08:30:00.001. Before, the
  digits were cut without a message: *equals* found 08:30:00, and *is greater than or equal to*
  kept it, while *does not equal* and *is less than* left it out.
- **Filter rows**: the same exact comparison for `2025-01-01 08:30:00.000000001` in a column of
  microseconds, for a date-time parameter such as `{Now}` in a column of milliseconds with a time
  zone, and for digits below a nanosecond.
- **Fill empty values**: a typed date-time with more digits than its column holds makes the
  column's unit finer, so that it keeps its digits and the other values stay as they are. When a
  value of the column doesn't fit the finer unit (nanoseconds reach only from 1677 to 2262), or the
  value has digits below a nanosecond, the step stops with a message. The same for a date-time
  parameter such as `{Now}`. Before, the fill cut the digits without a message:
  `2025-01-01 08:30:00.0005` was written as 08:30:00.
- **Filter rows** and **Fill empty values**: in a column with nanoseconds and a time zone, a typed
  date-time near 1677 or 2262 is the moment of its local time. East of UTC, the local time can be
  after 2262-04-11 23:47:16.854775807, the last moment of nanoseconds, while the moment is before
  it. West of UTC, the same happens before 1677-09-21 00:12:43.145224192.
- **Filter rows** and **Fill empty values** in 0.6.3 (new in that version): such a local time with
  digits below a microsecond became another moment without a message. It was 23 minutes 40 seconds
  off in Lord Howe and 3 minutes 58 seconds in New York. *Equals* found no row, *is less than* found
  the value itself, and the fill wrote the other moment.
- **Filter rows** and **Fill empty values**: a typed date-time in the last microsecond at either
  end of nanoseconds, such as `1677-09-21 00:12:43.145225` or `2262-04-11 23:47:16.854775807`, is
  inside them. The fill keeps a column of nanoseconds, and **Filter rows** compares to the
  nanosecond. One just after the last nanosecond, such as `2262-04-11 23:47:16.854775808`, is after
  every value of such a column; the fill says that its digits below a microsecond need
  nanoseconds.
- **Filter rows** and **Fill empty values**, the ends of nanoseconds in 0.6.3 (new in that
  version): the fill made the column microseconds and cut `.854775807` to `.854775` without a
  message. When a value of the column had digits below a microsecond, it stopped ("doesn't fit
  column …: nanoseconds reach only from 1677 to 2262"). **Filter rows** compared in microseconds:
  `1677-09-21 00:12:43.145225` equaled 00:12:43.145225001 too, and
  `2262-04-11 23:47:16.854775808` equaled the values of its microsecond.
- **Filter rows**: on a text column, *is greater than* and *is less than* (and *or equal to*)
  compare text, character by character, as before. With a value that reads as a number, a date or
  a time, such as `-16`, `31.03.2026` or `08:30`, the step now warns that the column holds text.
  Before, there was no warning: `-14,2` *is greater than* `-16` was false, and `8:15` *is less
  than* `08:30` too.
- **Filter rows**, this warning: for a number or a date it says that **Change type / format**
  before the step makes it compare numbers or dates. For a time, which **Change type** doesn't
  make, it names a formula for **Filter rows by formula**, such as
  `TRY_CAST([Start] AS TIME) < TIME '08:30'`.
- **Filter rows**, this warning: a number has a digit, so words such as `nan` or `inf` give no
  warning. Of many such conditions, the step warns for the first three, then in one line: "3,997
  more conditions compare texts character by character: 'c3', 'c4', 'c5', …". *Equals*, *is one
  of* and *contains* stay without a warning.
- **Merge**: lists and records (from Parquet files or plugins) match only keys of exactly their own
  type, value by value, as before. Against other keys, the step stops: "'Tags' holds list values,
  which can't be compared with 'CustNo': choose other key columns". A plugin's own values can't be
  keys at all.
- **Merge** in 0.6.3 compared a record as a text without its field names, so `{a: 1}` matched
  `{b: 1}`, also in a *Full join*. A *Full join* of a record of two fields against a text key
  stopped with a technical error. A list stopped with a message in Polars' words, and a plugin's
  values with a technical error.
- **Merge**, a *Full join* that matches one column of this table with two columns of the other
  table: when the first pair makes the column text, a row only in the other table gets the second
  key as text. That is the text of the value in its own column's type, with every digit: `2.5`,
  `0.1`, `17.00`, `2025-01-04`, `18446744073709551615`.
- **Merge**, such a *Full join* in 0.6.3: it stopped with "the keys … don't fit in one column" when
  the value didn't fit the column's first type (2.5 for whole numbers). It wrote the others in that
  type (`17` for `17.00`, `1.00` for 1 against exact decimals).
- **Append**, **Unpivot** and the folder imports: a date after 2262 (such as `31.12.9999`) beside
  date-times in nanoseconds with a time zone (as other programs write them to Parquet files) gives
  date-times in microseconds, which hold both. Before, **Append** and the folder imports made that
  date empty without a message, and **Unpivot** stopped with a technical error.
- **Fill empty values** with a fixed value leaves a column without empty cells as it is. Before, it
  could change the column's type (whole numbers became decimal numbers with the value `1,5`) or
  stop the step, although no cell got the value.
- **Fill empty values** with a value that a column of fixed values (a plugin's list of allowed
  values) doesn't hold stops the step. The message names the column's values: "'XL' doesn't fit
  column 'Size': it holds only these values: 'S', 'M', 'L'". Before, its empty cells stayed empty
  without a message.
- **Export to Excel file**: whole numbers and exact decimals with more than 15 significant digits
  stay numbers, and the step warns how many of them each column has. To keep all their digits,
  change the column to text before the export. Excel keeps 15 significant digits: the file holds
  9007199254740992 for 9007199254740993, and 1.234567890123457E+19 for 12345678901234567890.1234.
  Also when only one sheet of a workbook is replaced. Decimal numbers are not counted. Before,
  there was no warning.
- **Export to database** (SQL Server): the ODBC driver passes fractions of a second to the
  microsecond. When values have finer digits and their column could hold them (`datetime2`, `time`
  or `datetimeoffset` with 7 digits, as in a new table), the step warns how many values were cut,
  with an example. Before, the digits were lost without a word.
- **Export to database** (SQL Server): two keys that differ only below a microsecond are one key
  now, as the column holds them, so **Update** stops with the message about repeated keys. Before,
  an update by such keys changed the rows of the other key too.
- **Export to database** (SQL Server): categories (from a plugin, also categories with a name of
  their own) keep their characters in `char` and `varchar` columns in **Add the rows** and the
  replace modes, as texts do. Before, they went in this PC's code page without a message: `Łódź`
  became `Lódz` in a Polish column and `日本` became `??` in a UTF-8 one.
- **Export to database** (SQL Server): a category too long in bytes for a UTF-8 column now stops
  the export with SQL Server's message, which names the column. Before, it was cut (`ääää` became
  `ää` in a `varchar(5)`). **Update** and **Upsert** wrote categories right before.
- **Import CSV / text files** and **Import from database** (**Detect numbers and dates**): a
  date-time with nanoseconds keeps them (`2025-01-01 08:30:00.000000001`), as the SQLite export
  writes them. A column with a date-time beyond 1677 to 2262, the years of nanoseconds, keeps
  microseconds, and the step says that the digits below a microsecond were cut. Before, they were
  cut without a message, also when a SQLite export was read back.

### Table statuses

- **Redo** of deleting a table gives the tables that used it their error again ("Table … does not
  exist"), also when the Redo comes after the calculation of the **Undo**. Before, they kept their
  "ok" until you selected them.
- On the map, **Del** on arrows into tables that are not selected (arrows selected with Shift+drag
  or Ctrl+click), followed at once by Ctrl+1 (or **Tables**), no longer leaves those tables and the
  tables that use them with their old status. Before, a table without steps could still show
  "ok".
- **Rename** a table: the tables that use it show the new name in their messages ("uses “Sales”,
  which is turned off"). A step that names the table in a project file written by hand uses no
  table now, as it says. Before, they kept the old name and their old status until you selected
  them.
- **Undo** and **Redo** of moving tables on the map, of groups and of notes calculate no table
  again: nothing that a calculation reads changed. Before, the moved table and the tables that use
  it were calculated again.
- **Tables that are off** in a circle (see **Results that change**): the message of a skipped table
  names the way to the table that is off, for example "uses “C”, which is turned off". It follows
  edits of the tables on that way. Without a circle, the messages are as before.
- **Circular reference**: each table of a circle names the circle from itself, for example
  "Circular reference: A → B → A" for *A* and "B → A → B" for *B*. This holds whichever table you
  select first, and whichever tables `tafl run` runs. Before, all tables of a circle named it from
  the table calculated first.
- **Circular reference**: when an earlier step of table *A* of a circle fails (a missing file), *A*
  keeps that error, and *B* still names the circle. Before, *B* said "Table 'A' has an error: …" or
  "Circular reference: A → B → A", by the order.
- On the map, the box of a deleted table (shown a moment longer) has the status of its own result.
  This holds also when another table has the deleted table's id as its name (a table named `t5` in
  a project file written by hand). Before, it could show the status of that other table, for
  example "off".

### Steps that work now

- **Number rows**: with *Number the groups*, **New column** can be one of the group columns: the
  numbers replace that column in its place, as they do with *Number the rows*. Before, the step
  stopped with `ColumnNotFoundError: "Region" not found`. Another column of that name is replaced
  as in 0.6.3: the numbers come after the other columns, so stored projects keep their column
  order.
- **Formulas**: two exact decimals with different decimal places can be compared and added, for
  example `[Big] = [Rate]` with 12345678901234567890123 and 1.500000000000000000 (18 decimal
  places, as Parquet files of other programs often have them). Before, the formula stopped with
  "Casting value "12345678901234567890123" to type DECIMAL(38,18) failed: value is out of range!".
- **Formulas**: when such values need more than 38 digits together (23 digits and 18 decimal
  places in use), the formula stops with the plain message "the result needs more than 38 digits,
  decimal places included, …". So does `CAST([Big] AS DECIMAL(38, 18))`.
- **Formulas**: exact decimals of other widths work too: `[Amount] + [Rate]` of a DECIMAL(18,2)
  with 123456789012345.67 and a DECIMAL(18,4), and `[Id] + 1.5` of a DECIMAL(18,0) with 18 digits.
  Their results are decimal numbers, as for other exact decimals with decimal places. Before, these
  stopped with "Casting value "123456789012345.67" to type DECIMAL(18,4) failed: value is out of
  range! …".
- **Formulas**: a time of day can be compared with a time and gets an interval:
  `[Time] > TIME '12:00:00'`, `[Time] BETWEEN TIME '07:00' AND TIME '09:00'`,
  `[Time] + INTERVAL 1 HOUR` and `DATE '2025-01-01' + [Time]` work. Before, they stopped with
  "Cannot compare values of type TIME_NS and type TIME - an explicit cast is required". BETWEEN
  and `+` stopped with messages of the same kind ("Cannot mix values of type TIME_NS and TIME …",
  "No function matches … '+(TIME_NS, INTERVAL)' …").
- **Formulas** on a time column with digits below a microsecond: its times keep them. The message
  says what to write: "[N] holds times with nanoseconds, which a formula compares with a time only
  by = and IN: write CAST([N] AS TIME), or a time as TIME_NS '12:00:00'". A formula with TIME_NS
  compares to the nanosecond, as before.
- **Clean text** and **Replace** work on time spans, and on binary values that are no text. Before,
  these stopped the step with a technical error (in **Replace**: "Invalid regular expression:
  invalid utf8").
- **Change type** to *Text*: a time span becomes `1d 2h 3m`, as **Pivot** writes it. Before, the
  step stopped with "casting from Duration('μs') to String not supported". Dates with a format that
  has a time, such as `dd.mm.yyyy hh:mm`, are written at midnight, `31.12.2025 00:00`, as Excel
  does. Before, the step stopped with a technical error that named an internal `col("value")`.
- **Export to CSV** writes time spans as text that is the same on every PC (`1d 2h 3m`, as
  **Pivot** writes them). Before, a column of time spans stopped the export with "datatype
  duration[μs] cannot be written to CSV".
- **Export to database** (SQLite) finds the rows by up to 1,999 key columns. Before, it stopped
  with "Expression tree is too large (maximum depth 1000)". This came with 500 key columns in
  **Replace the rows with the same key values** and **Delete the rows with the same key values**,
  and with 999 in every mode by keys.
- **Export to database** (SQL Server) finds the rows by more than 32 key columns (16 before SQL
  Server 2016). Before, it stopped with "The index 'tafl_stage_…_keys' on table '#tafl_stage_…'
  has 40 columns in the key list. The maximum limit for index key column list is 32.".
- **Export to database** (SQL Server), **Delete the rows with the same key values**: decimals in a
  column that isn't a key no longer stop the delete when the database column holds whole numbers.
  Only the keys are written. Before: "The column Price of … holds whole numbers (int), but Price
  has decimals (e.g. 1.5): they would be cut off …".
- **Export to database** (SQL Server): a new table holds whole numbers beyond
  9,223,372,036,854,775,807 (unsigned 64-bit numbers of a Parquet file): their column is
  `decimal(20, 0)`, or `decimal(38, 0)` for 128-bit numbers of a plugin. Such numbers also go into
  an existing column that holds them: a text column gets their digits, a decimal column with enough
  digits the exact value. Before, the export stopped with "int too big to convert", or for a
  `decimal(19, 0)` column with a message about the TDS protocol.
- **Export to database** (SQL Server): texts and other values go into columns of the old types
  `text` and `ntext`, in every mode. Numbers, dates and true/false are written as **Add the rows**
  writes them (`1`, `1.5`, `2025-12-31`, `1` for true), also categories and columns whose first
  value is empty.
- **Export to database** (SQL Server), `text` and `ntext` columns in 0.6.3: every export of texts
  into such a column stopped with "There is not enough memory for this. Close other programs, then
  calculate again." So did categories and columns whose first value was empty in **Add the rows**.
  **Update** and **Upsert** of numbers and dates stopped with "Operand type clash: int is
  incompatible with text".
- **Import CSV / text files**: a file whose lines end with a carriage return alone (as some old
  programs write them) is read. Before, it gave no rows and no message.

### Plain messages

- **Formulas**: a date function on a column of text, such as `QUARTER([Date])`,
  `STRFTIME([Date], '%Y')`, `DATE_DIFF('day', [Date], DATE '2026-12-31')` or
  `EXTRACT(YEAR FROM [Date])`, says what to do. The message is "QUARTER needs a date, but [Date]
  holds text: change its type to a date before this step, or write CAST([Date] AS DATE)".
- **Formulas**, date functions on text in 0.6.3: the message was DuckDB's, "No function matches the
  given name and argument types 'quarter(VARCHAR)'. You might need to add explicit type casts."
  For STRFTIME it was "Could not choose a best candidate function …".
- **Formulas**: `[Date] > DATE '2025-01-01'` on a column of text says "[Date] holds text, not a
  date: …". So do `[Date] BETWEEN DATE '2025-01-01' AND DATE '2025-12-31'` and
  `COALESCE([Date], DATE '2025-01-01')`. Before, DuckDB said "Cannot compare values of type VARCHAR
  and type DATE - an explicit cast is required" or "Cannot mix values of type VARCHAR and DATE …".
- **Formulas**: in a formula with more columns of text, the hint names the column that needs
  another type. A function or operator that needs numbers, such as `ROUND([A], 2)` or `[A] * 2`,
  says so the same way.
- **Formulas**: `[A] + [B]` on texts says "[A] and [B] hold text: to join text use || or CONCAT();
  to add numbers, change their type to numbers before this step, or write CAST([A] AS DOUBLE)".
  Before, it gave DuckDB's message "No function matches the given name and argument types
  '+(VARCHAR, VARCHAR)' …", with "— to join text use || or CONCAT()" at its end.
- Lists, records and a plugin's own values stop **Join texts**, **Clean text**, **Replace** and
  **Change type** to *Text* with a plain message. Examples: "“Tags” holds list values, which Join
  texts can't join", "Tags: the column holds list values, which can't become text". Before, lists
  and a plugin's values stopped these steps with a technical error. Records became text such as
  `{1,"a"}`, without their field names.
- **Change type** of dates or date-times to *Text* with a number format, such as `0.00`, `0.000`,
  `#,##0.000`, `00000` or `#,##0`, stops with a plain message. So does a format that writes no part
  of a date (`hh:mm` for dates). The message is "When: the format '0.000' writes no part of a date.
  Use codes such as dd.mm.yyyy hh:mm:ss".
- **Change type** to *Text* with such formats in 0.6.3: `0.00`, `#,##0.00`, `0%` and `hh:mm` for
  dates stopped with a technical error that named an internal `col("value")`. `0.000`,
  `#,##0.000` and `0.000000` wrote the fractions of a second (`0.500`, `#,##0.500`) without a
  message, and `00000` or `#,##0` gave that text for every date.
- **Change type** of date-times to *Text* with a fraction of a second that can't be written, such
  as `hh:mm:ss.00`, stops with "When: date-times can't be written with the format 'hh:mm:ss.00'.
  Use codes such as dd.mm.yyyy hh:mm:ss, and .000, .000000 or .000000000 for fractions of a
  second". Before, it stopped with a technical error.
- **Change type**: the examples in its warnings are different values: "e.g. 'x', 'y', 'z'". Before,
  a value could be named twice ("e.g. 'x', 'x', 'y'").
- **Change type**: a column that can't become a number, a date or true/false is named in plain
  words: "W: a date-time column can't become a number". Before, the message named the technical
  type, such as "a Datetime(time_unit='us', time_zone=None) column".
- **Filter rows** on a column of lists or records (a Parquet file can have them): for every
  operator except *is empty* and *is not empty*, the step says that it can't compare the column
  with a value. It names a formula for **Filter rows by formula** that can, such as
  `LIST_CONTAINS([Tags], 'a')`, `LIST_CONTAINS([Ids], 1)` or
  `STRUCT_EXTRACT([Person], 'name') = 'a'`.
- **Filter rows**, this formula: its value is of the type of the items or of the record's first
  field, and `LEN([Tags]) > 0` where a type has no such value. A column of a plugin's own values
  gets a plain message too.
- **Filter rows** on lists, records and a plugin's own values in 0.6.3: most operators stopped with
  a technical error ("cannot cast List type", "'is_in' cannot check for Struct…", "cannot cast
  'Object' type"). Some compared without a message: *is one of* took the typed values as one list
  (`a;b` kept only the list [a, b]), and *equals* and *is greater than* compared records as text.
- **Fill empty values** with a fixed value on a column of lists or records says that a typed value
  can't be used with it, as for time spans. Before, it stopped with a technical error ("cannot
  cast List type", "conversion from `struct[2]` to `str` failed"), or made every record text, such
  as `{1,"a"}`, without a message. *Fill down* and *fill up* work on such columns as before.
- **Export to CSV**: columns of lists or records stop the export with a plain message that names
  them ("A CSV file can't hold lists or records: Tags and Pair have them. Remove these columns
  before this step …"), and the file stays as it was. Before, the message was "CSV format does not
  support nested data", without the columns.
- **Export to database**: values that SQLite or SQL Server can't store stop the export with a plain
  message that names the column, before anything is written (a new table isn't made). These are
  time spans, lists, records and values of a plugin's own type, and in SQLite whole numbers beyond
  9,223,372,036,854,775,807 and a table of more than 2,000 columns.
- **Export to database**, such values in 0.6.3: the messages were the drivers' words, such as
  "Error binding parameter 1: type 'datetime.timedelta' is not supported", "Python int too large to
  convert to SQLite INTEGER" or "too many columns on wide". For a new SQL Server table they were
  even "casting from Duration('μs') to String not supported" or "Unknown object type dict during
  describe".
- **Export to database** (SQL Server): a whole number outside the range of its column of whole
  numbers stops the export with a message. It names the column, its range (`bit` 0 and 1,
  `tinyint` 0 to 255, and so on for `smallint`, `int` and `bigint`) and a type that holds the
  values. Before: "A value doesn't fit its column in the database: a number is too large for its
  column, or has too many digits", without the column, or "int too big to convert".
- **Export to database** (SQL Server): whole numbers beyond 9,223,372,036,854,775,807 (from a
  Parquet file or a plugin) that a column can't hold stop the export with a message that names the
  column. A `float`, `real`, `money` or narrower `decimal` column gets the advice of a wider type,
  a `sql_variant` column the advice to make the data's column text. Before: "int too big to
  convert".
- **Export to database** (SQL Server): columns of dates, times, ids, xml or binary values say that
  they can't take whole numbers beyond 9,223,372,036,854,775,807. Whole numbers of more than 38
  digits stop the export, also before a new table is made, with the advice to keep them as text.
- **Import CSV / text files**: a quote out of place stops the import with a message that names and
  shows the line, and says how such a value is written in a CSV file. That is a quote in a value
  that doesn't start with one (`24";Monitor`), or text after the closing quote (`"24"x`). A value
  with a pair of quotes (`24" and 27"`) is read as before.
- **Import CSV / text files**, a quote out of place in 0.6.3: the message was "CSV malformed:
  expected 3 rows, actual 5 rows, in chunk starting at row_offset 3, length 39". With text after
  the closing quote, it was "could not parse `"24"x` as dtype `str` at column 'column_0' (column
  number 1)".
- **Import CSV / text files**: a value that starts with a quote that is never closed (`1;"Mouse;5`)
  stops the import with a message that names and shows the line. Before, the message was "could
  not parse `"Mouse;5`", without the line.
- The grid shows *list*, *record* and *object* under the names of columns of lists, records and a
  plugin's own values, and *text* for categories (from plugins). Messages use these words ("“Tags”
  holds list values, which can't be split"). Before, they showed Polars' names, such as
  `list(string)`, `struct({'a': int64})`, `categorical` and `enum(categories=['a', 'b'])`.
- **Merge**: the warning about keys compared as text, and the messages about keys that can't be
  compared or don't fit in one column of a *Full join*, name the types with the words of the grid.
  Examples: "'Customer' (integer) and 'CustNo' (text) are compared as text", "'A' (duration) and
  'K' (text) can't be compared: …". Before: `(Int64)`, `(String)`, `(Duration(time_unit='us'))`.
- **Merge**: categories against text keys give no warning, as both types are called text. Before:
  "'A' (Categorical) and 'K' (String) are compared as text".
- **Merge**: the message about rows without a match names their keys as **Pivot** writes them:
  `17`, `10000000000000000`, `false`, `08:30:00.500`, `1d 2h`, a time zone's local time. Binary
  values are shown as their text when all bytes of the key column are text (`abc`, as **Merge**
  compares them with text keys), else as hex digits (`0x00FF`).
- **Merge**, these keys in 0.6.3: `17.0`, `1e+16`, `False`, `b'abc'`, `b'\x00\xff'`,
  `08:30:00.500000`, `1 day, 2:00:00`, `2025-07-01 09:30:00+02:00`.
- **Replace**, *Whole cell value*: when a column becomes text, the warning names the value that
  isn't one of its type, in plain words. Examples: "'n' was converted to text because 'zero' isn't
  an integer", "… because '1,12345' has more digits than the column's exact decimals hold".
- **Replace**, *Whole cell value*: a column of times, time spans, binary values or no values says
  "'v' was converted to text: Replace keeps the type only of numbers, dates, date-times and
  true/false values". Before: "… because a value isn't a Decimal(precision=30, scale=4)", and "…
  isn't a Time" also when **Find** and **Replace with** were times. Categories give no warning.
- **Aggregate** and **Pivot**: messages about the type of a column have the right article: "O is an
  object column". Before: "a object column".

### Speed

- **Fill empty values** with a fixed value on 4,000 columns of 10 rows takes 0.07 to 0.19 s. 0.6.3
  took about 35 s for text, 44 s for numbers and 66 s for date-times. On 2,000 text columns it
  takes 0.10 s (0.6.3: about 9 s), and *fill down* of 4,000 columns 0.15 s (0.6.3: about 9 s).
- **Filter rows** with a condition for each of 4,000 columns of 10 rows: *equals* takes 0.13 s
  (0.6.3: about 9 s), and 0.17 s with date-times (0.6.3: about 22 s). *Contains* on 4,000
  decimal-number columns of 8 rows takes 0.17 s (0.6.3: about 9.5 s).
- **Fill empty values** and **Filter rows** in 0.6.3: every column and condition made the table's
  schema anew, and every condition read its typed value again. So the time grew with the square of
  the number of columns.
- **Filter rows**: *contains*, *starts with* and *ends with* on long tables with many decimal-number
  columns take longer than in 0.6.3, as each value gets its exact text (`17`, not `17.0`). They
  take up to about five times as long: 100,000 rows and 200 such columns 0.9 s (0.6.3: 0.2 s),
  300,000 rows and 300 columns 1.9 s (0.6.3: 0.7 s).
- **Replace** on 4,000 decimal-number columns takes about 1 s (0.6.3: about 9 s). **Clean text** on
  them takes longer than in 0.6.3, as each value gets its exact text: about 1.2 s (0.6.3: 0.25 s).
- **Import Parquet files** reads files with thousands of text columns as fast as 0.6.2 did. 4,000
  text columns take 0.05 s (0.6.3: 9 s), and 20,000 take 0.24 s (0.6.3: about 5 minutes). A
  folder of three files of 4,000 columns takes 0.12 s (0.6.3: 3.3 s).
- **Instant filters**: the step that **Add as step** makes of many time periods of a column with a
  time zone is calculated faster. On a million rows, 122 years with 5 left out take 0.07 s (0.6.3:
  0.28 s), and 5,000 separate dates 0.28 s (0.6.3: 0.74 s).
- **Instant filters**: on a million rows, **Add as step** with its check takes about 0.1 to 0.25
  seconds (600 separate months: 0.43 s; 20,000 stored values that aren't of the column's type:
  0.85 s). Formulas with long lists of values, such as the step of a filter of 20,000 texts, take
  0.83 s instead of 1.64 s.

### Tools and plugins

- (Source installs) `tools\make_big_project.py` needs the folder for the project
  (`… make_big_project.py %TEMP%\big`). Before, without one it wrote `examples\big500.tafl` into
  the Tafl folder.
- (Development) `tools\check_statuses.py` checks the statuses and messages of the tables with
  random projects and edits. It also uses **Skip the rest if a table is empty**, a parameter in a
  file name and files that change (see Development in the README).
- Plugin API, additions (it stays at version 1): `tafl.api.detection` finds numbers and dates in
  text columns as the CSV import does (`guess_decimal`, `detect_types`).
- Plugin API, `tafl.api.dtypes`: `common_type(columns)` gives the type in which Unpivot and Append
  put the values of several columns (a `CommonType` with `dtype`, `counted` and `lost`).
  `to_common_type(expr, dtype, target)` converts a column to it (with the keyword `text_bytes`,
  binary values that are all text as that text).
- Plugin API: `dtypes.stack(frames, warn=None, sources="tables")` puts frames below each other with
  that rule and its warnings, as Append and the folder imports do. `tafl.api.files.combine` has a
  new keyword, `warn`, for those warnings (without it: the same types, no message).
- Plugin API, more in `tafl.api.dtypes`: `numbers_to_datetimes(s, fmt)` and `is_digit_format(fmt)`
  read numbers as dates as Change type does. `rounded_to_whole(s, decimal)` tells which values a
  whole number keeps only rounded, and `parse_whole_numbers(s, decimal)` gives the whole numbers of
  texts and the rounded ones from one reading.
- Plugin API, `tafl.api.dtypes`: `type_word(dtype)` and `with_article(text)` give the words of
  messages. `type_label` calls lists *list*, records *record*, a plugin's own values *object* and
  categories *text*. `excel_serial_to_datetime` gives empty values for day numbers outside the
  years 1 to 9999, NaN and infinity; before, it stopped with an error.

## 0.6.3 – 2026-11-04

This version fixes wrong results and errors with unusual data: columns named `*` or like `^…$`, very long
whole numbers, nanoseconds, time zones, binary values and values of a plugin's own type. Table
statuses stay right after more kinds of edits, and more errors are plain messages.

- **Column names `*` and `^…$`**: a column named `*` (a rating, a flag or a footnote star, also one
  that **Pivot** makes from such a value), or with a name that starts with `^` and ends with `$`,
  is now taken by its own name in every step, import, export, filter and data sheet. Before, Tafl
  read such a name as all columns, or as a pattern (`^a.*$` found the column `abc`), without a
  message. **Remove columns** `*` removed every column. **Sort** and **Number rows** sorted by all
  columns or by another one. **Remove duplicates**, **Aggregate** and **Merge** compared other
  columns. **Fill empty values**, **Replace**, **Clean text** and the Excel export could write the
  values of another column into it, also into a column of lists or records. **Rename files** could
  rename the files listed in another column. Many other steps stopped with an unclear error, such
  as "IndexError: index 0 is out of bounds": the imports that detect types, **Sort** by a text
  column, **Pivot**, **Change type**, sums in **Aggregate**, **Append**, **Unpivot**, **Split
  column**, the CSV, Excel and database exports, and data sheets.
- **Import from database**: date-times, date-times with a time zone and time spans of a column
  whose name starts with `^` and ends with `$` keep their type. Before, they became text.
- A change in **Parameters** or **Connections** checks every table again, and so do **Undo** and
  **Redo** of that change: a table that reads `{Name}.csv` or a database of the connection, and
  the tables that use it, no longer keep their old status until they are selected.
- An edit followed at once by **Duplicate**, **New table**, **New table from this one** or an arrow
  on the map no longer leaves the edited table and the tables that use it with their old status.
  The same for **Run this table** just after a change of that table (for example Shift+F5 while you
  type a setting): the tables that use it, and were not in the run, show their new status too.
- A step whose table was deleted no longer uses another table whose name looks like the deleted
  table's id. Before, a table named `t3` gave its data to the step of a deleted `t3` without a
  message, and a table named *T3* got an arrow on the map. A new table no longer gets the id of a
  deleted table that a step still names. The step says that its table doesn't exist, in the window
  and in `tafl run`, and the map draws no arrow for it. In a project file that you write yourself,
  a step can name its table instead of giving its id, in any case (`customers` for *Customers*); a
  text such as `t3` is always an id.
- The project map no longer shows an error box ("There is no table …") when a table is deleted,
  or removed by **Undo**, while tables are being calculated.
- After a run that stopped with an unexpected error, the status bar no longer says "Running the
  project…". If you closed the window during that run and then clicked **Cancel** at the question
  about unsaved changes, it no longer says that the window closes.
- **Import from database** keeps every digit of long whole numbers: whole numbers of more than 15
  digits beside decimal numbers in a SQLite column, and whole numbers beyond
  9,223,372,036,854,775,807 that the database driver of a plugin gives (for example for a 128-bit
  column). They become exact decimals, or text beyond 38 digits or beside a very small decimal
  number such as `6.62607015e-34`. Before, the SQLite numbers were rounded, and a driver's value
  beyond about 1.7 × 10^38 became empty, without a message.
- **Import from database**: a column with exact decimals beside text (possible with the database
  driver of a plugin) becomes text, as other mixes do. Before, the import stopped with a technical
  error.
- **Import Parquet files** reads whole numbers of 128 bits (other programs can write them): as
  whole numbers when they fit, else as exact decimals, or as text with a warning beyond 38 digits. Before, a value beyond 9,223,372,036,854,775,807 stopped
  the step with a technical message.
- **Formulas** work on whole numbers of more than 64 bits. Before, every formula on such a column
  stopped with a technical error. A result above
  170,141,183,460,469,231,731,687,303,715,884,105,727 stops the formula with a plain message,
  instead of giving a wrong negative number.
- **Formulas**: a whole number beyond 9,223,372,036,854,775,807 that a formula gives, such as a big
  sum, keeps all its digits, as an exact decimal (up to 38 digits), and a parameter's formula keeps
  it as a whole number. Before, it became a decimal number, rounded to about 15 digits, without a
  message. A formula whose result has more than 38 digits, such as `[Total] * [Total]` on such a
  sum, stops with a plain message: write `CAST([Total] AS DOUBLE) * [Total]` for a decimal number.
  In 0.6.2, such a formula gave a rounded decimal number.
- **Aggregate** and **Pivot**: a sum of exact decimals beyond 38 digits stops the step with a plain
  message. Before, it gave a wrong number in a group, and a technical error for a total row, also
  when only a partial sum was that long.
- **Filter rows** and **Fill empty values** work on date-times with a time zone (a Parquet file can
  have them, for example in UTC). Before, the step stopped with a technical message. A typed
  date-time is the local time of the column's time zone, as the grid shows it, and `today` and
  `now` are the date and the time in that zone. A time that a clock change skips or repeats in
  that zone stops the step with a message that says so. A value that is only a date-time
  parameter, such as `{Now}` with `=NOW()` (also from `tafl run --set`), is that moment of this
  PC's clock, as in **Filter rows by formula**. A fill that would put a moment before the year 1 or
  after the year 9999 in the column's zone stops with a message.
- **Filter rows** and **Fill empty values**: a typed time or date-time keeps its nanoseconds
  (`08:30:00.000000001`) on a column that has them (Parquet files of other programs often do).
  Before, it was cut to microseconds: `2025-01-01 08:30:00` also found `08:30:00.000000001`, and a
  fill made the whole column lose its nanoseconds. A date-time before 1677 or after 2262, such as
  `31.12.9999`, is beyond nanoseconds: **Filter rows** compares it in microseconds, and a fill
  makes the column microseconds, as before. When the column has nanoseconds that this would cut,
  the fill stops and names one.
- **Filter rows** (contains, does not contain, starts with, ends with) and **Merge** (a binary key
  against a key of another type) work on binary values that are no text: such a column is used as
  hex digits (`0xFF00`), as **Join texts** writes it, and bytes that are all text stay text.
  Before, the step stopped with "invalid utf8".
- **Filter rows**: a whole number below 0 is below all values of a column of very wide unsigned
  whole numbers (UInt128, for example from a plugin). Before, "is greater than" left out the
  values from 2^127 up.
- **Fill empty values** no longer changes long whole numbers without a message. A fixed value with
  decimal places, or a whole number too big for the column's type, stops the step when decimal
  numbers would change the column's numbers or the typed number, and the message names a number
  that would change (9223372036854775807 would become 9223372036854775808; a typed
  `1234567890123456.78` would become `1234567890123456.8`). Before, the column became decimal
  numbers, and a fill with `12345678901234567891` wrote `12345678901234567168`.
  **Change type** to a decimal number now warns about every rounded whole number of 128 bits.
  Before, it stopped with a technical message for unsigned ones from 2^127 up, and it said nothing
  about the highest signed ones.
- **Fill empty values**: a number below 0 in a column of unsigned whole numbers that holds values
  too big for a column with numbers below 0 now says so, and names the highest number the column
  could hold. Before, the step stopped with a technical message.
- **Instant filters**: **Add as step** of a filter by week keeps the same rows for years before
  1000: the week of 1 January 0001 is now `0001-W01`, as the step writes it. Before, the step kept
  no row. A filter by week that 0.6.2 saved for such a year keeps its rows.
- A column of a plugin's own values has no filter in the menus of the grid or in **+ Filter**,
  because such values can't be compared. Before, **Show only rows where …** hid all rows and the
  project couldn't be saved, or an error came up. A filter made where the column holds texts is
  not used where it holds such values.
- **Grid**: a value of a plugin's own type that has no text shows "(no text)", and Ctrl+C copies
  that. Before, an error box came. Ctrl+C puts category values, and values of a plugin's own type,
  that have a tab or a line break in quotes, as it does with texts, so that Excel keeps each one in
  one cell.
- **Step settings**: a whole number that is too big for its field, from a project file written by
  hand (above 2,000,000,000 in a number field, or beyond 9,223,372,036,854,775,807 in any setting,
  also in a choice such as **Delimiter** or in a row of a list), shows and stays until you change
  it, also when another setting of the step changes. In a choice, the step says that the number is
  not one of its options. Before, an error box came, the change was lost, or the number became
  2,000,000,000 or `-1` without a message.
- **Import CSV / text files**: with the delimiter **Detect automatically**, a file whose first
  column name has a line break in quotes (a header cell of two lines) is read. Before, it said
  `could not parse`, or it read the file as one column. A quoted value that holds a line end
  converted twice (`\r\r\n`) stays as it is when no line end outside quotes was converted twice,
  for example in a file that Tafl or a data sheet writes. Before, it lost a `\r`. A file with such
  line ends outside quotes is read
  with normal line ends, as before, also when only some rows have them (for example after a
  header written with other line ends).
- **Export to Excel** writes values of a plugin's own type as text. Before, such a column stopped
  the export with a technical error. A list or record whose text is longer than a cell holds is
  cut with the same warning as a long text; before, it was cut without one.
- Plugins: `col(name)`, `cols(names)` and `selector(names)` from `tafl.api` take columns by their
  exact names. Use them for the columns that settings or the data name, instead of `pl.col(name)`
  or names as text (see **Writing a plugin** in the README). Import them by name
  (`from tafl.api import P, action, col`): `from tafl.api import *` leaves them out, so that it
  doesn't replace Polars' own `col`. The functions of `tafl.api.dtypes` work on a Series of any
  name, also one named like `^a.*$`.
- Plugin API, more additions (it stays at version 1): a text setting that is only a date-time
  parameter is a `tafl.api.parameters.DateTimeText`, a text that has the value too (`.value`).
  `tafl.api.parameters.fill_setting(text, values)` fills in a text setting as a step gets it, and
  `tafl.api.expressions.moment` reads a date-time of this PC's clock as formulas do.
  `tafl.api.dtypes.bytes_are_text(values)` says whether binary values are all text, and
  `tafl.api.dtypes.parse_datetimes` has a new keyword, `time_unit`.

## 0.6.2 – 2026-11-03

- A table that uses a changed table no longer keeps its old status when Tafl finishes checking the
  other tables just after a change: for example a table turned off or deleted while **Reload data**
  or the **Problems** filter still checks the other tables, or an **Undo** or **Redo** during a
  calculation.
- **Add as step** in the filter pane keeps the same rows as the filter: for decimal numbers with
  many digits (a value such as `0.9931027217047139` found no row), for very small exact decimals,
  and for number ranges on a column of 32-bit decimal numbers or of very wide exact decimals (the
  step stopped).
- **Filter rows**: a typed number that is no whole number of the column, such as
  `9223372036854775808` on a whole-number column, keeps all its digits: `=` found the values near
  it.
- **Aggregate**: **Join texts** works on time spans, and on binary values that are no text (as hex
  digits), where the step stopped.
- A formula can't use `setseed`: it fixed the random numbers of the projects opened after it.
- A project file with half of a character in it (a lone surrogate such as `\ud800`, which
  another program can write) opens with `�` in its place, and can be saved again. Before, a save
  stopped with a message about surrogates. Two names or settings that differ only in such halves
  become the same.

## 0.6.1 – 2026-11-02

A project from somewhere else is now read first and trusted after that. The rest is fixes: text
stays exactly as it is in Excel files and in the window, SQLite imports only read, and runs need
less memory.

- **Protected view**: a project file that Tafl doesn't know opens so that you can read all its
  tables, steps and settings, the map and the Find panel, but nothing is calculated, no path is
  looked at and no database is opened. A bar at the top of the window says so. **Trust this
  project** in the bar makes the project work as always, and a run asks first. The demo opens this
  way too: click the button once.
  - Tafl knows a file by its content and its folder, in a list in the settings of this computer. A
    file that was changed elsewhere or moved to another folder asks again, and a project that is
    saved in protected view stays protected.
  - New projects are trusted, and a file you save from a trusted project is known.
  - The projects in **File → Open recent** count as known once, when this version starts for the
    first time.
  - `tafl run` and `tafl show` work as before: you start them on purpose.
  - The file dialog of a path setting starts in the project folder, not at the path.
  - "Projects from other people" in chapter 3 of the user guide says what trust means, and what it
    doesn't: a SQL Server query that commits by itself keeps its changes, and a rollback can't undo
    that.
- **The window can no longer hang during a calculation**: Python's clean-up of old objects could
  run on a calculation thread and wait for the window, while the window waited for it. It now runs
  on the window's thread only.
- **The project map can no longer lock the window**: at some window sizes the automatic fit and the
  scroll bars pushed each other without end.
- **A project that you open during a calculation or a run** no longer gets results of the project
  before, and the Run results keep the names of the tables of their own project.
- **A run always reads a database again.** After Stop, after a run that failed, or in a project
  opened in the same window, a run could take the rows of the read before from memory: its export
  then held old data, with no error. A project that you open again shows the new data too.
- **Run this table** is off during a run: it ended the run without a word, and the rest of the
  run didn't happen.
- A table that you change during a run is calculated again after the run, so that it keeps no
  status of the run. A filter, a note or a place on the map is no such change.
- **Reload data** during a run stays in force: at the end of the run, no table goes back to the
  older data of the run.
- After Stop, after a run that failed, and after a run of a project that is closed by then, the
  window no longer keeps an extra copy of the data of that run. Only the cache keeps it, within
  its limit.
- A table that you change just before you click another table no longer keeps its old status.
- **Excel export: text stays text**, exactly as it is, in the cells and in the column names, in a
  new file, a replaced file, a replaced or added sheet and a workbook with macros. No text becomes
  a formula or a link any more, and a text with special characters can't move other values or
  damage the file.
  - **A column of web addresses is no longer clickable in the file.** In return, no address is
    lost: very long addresses, and addresses in a table with very many of them, left empty cells,
    and `mailto:` and `file:` lost their prefix.
- **Excel export**: column names that differ only in upper and lower case, such as `Name` and
  `NAME`, now stop the export with a message, before it touches a file. An Excel table can't hold
  them: the step said it had written the rows, the cells were not written, and **Replace only this
  sheet** lost the old sheet. Control characters in column names no longer damage the file.
- **Replace only this sheet**: a workbook with very big drawings or charts no longer causes a long
  wait that nothing could stop, and a workbook with a part of 2 GiB or more works.
- **The window shows names and values as plain text**: in labels, tooltips and message boxes, a
  text from a project or a data file is never read as HTML. Normal text looks as it did, with its
  `&`, `<`, quotes and line breaks. Ctrl+C in a message box copies the text as it reads. A tooltip
  stops after 12 lines with "…", and the tooltip of a cell or an error after 40.
- **SQLite imports only read**, in the window and in a run, and so does **Test connection**.
  - A file that a program left unfinished stays as it is, and the message says how to repair it.
  - A WAL database is read with its `-wal` file, which stays as it is. SQLite can put its small
    `-shm` and `-wal` files beside a WAL database to read it.
  - A query that changes data stops with a message, and so do `ATTACH` and a PRAGMA that does more
    than read. Plain reads, such as `PRAGMA table_info`, work. An `ATTACH` in an import used to
    work in a run: put such work into a **Run SQL command**.
- **SQL Server connections**: a value in **Server** or **Database** can't add anything to the
  connection. A **Server** with `;`, `=`, `{`, `}` or a control character stops with a plain
  message. `.\SQLEXPRESS`, `host,1433` and `host\instance` work as before. Write a **Database** name
  as it is, without braces.
- **Stop and low memory now end a database import** between its rows: a read no longer goes on
  until the last row. On SQLite, Stop also ends a query or a **Run SQL command** that gives no
  row for a long time (the command is rolled back). SQL Server can stop a query only when rows
  arrive. When memory runs out in a step, the step says so in plain words.
- **Formulas** are read exactly as DuckDB reads them, and DuckDB confirms each formula just before
  it runs: it must be one `SELECT`. What one project's formula does can't reach the next project.
  A formula can't read DuckDB's settings or run a text as a query any more. Each formula takes about
  1 ms longer.
- **Formulas need less memory**: DuckDB gets only the columns a formula uses (a formula on one
  column of a table of 2 million rows, 30 columns and 200 parts took 646 MB and now takes 207 MB),
  and a calculated column keeps room for its own rows only.
- **`tafl run` needs less memory on long tables**: a step lets go of its data once the next step
  has run, and only the last step of a table keeps it. The window keeps its results as before.
- **Filter rows**: time columns work, with values like `08:30`, in all operators; text that is no
  time gives a clear message. **Is one of** on a whole-number column leaves out values that the
  column can't hold, such as `1,5`, instead of stopping. **Fill empty values** takes times too: a
  fixed value for a time column must now be a time. Very big whole numbers keep all their digits in
  both steps. A column of time spans says that a typed value can't be used with it.
- **Merge**: a full join on several keys works with any suffix, also an empty one.
- **Import from a database**: a SQLite column with a number or a text beside a binary value
  becomes text, with the binary value as hex digits (`0x0102FF`). The number 5 became five zero
  bytes, with no error, and other mixes stopped the import.
- A cell of binary data shows its first 500 bytes, and a list or a record its first 500 characters:
  one big cell made the grid wait at each paint. Ctrl+C still copies the whole value.
- **Show all** on the project map shows all of a big project also right after it opens, down to
  the smallest zoom (5 %). It stayed at 30 %. After Show all, the map keeps all of the project in
  view when the window changes size or groups open and close, until you zoom or move the map or
  a box.
- **Import CSV / text files**: the message on decimals names only columns that became numbers. A
  file with very long runs of line ends no longer causes a long wait.
- A file with thousands of equal column names no longer takes minutes.
- **Filter pane**: a filter that a project file holds in a wrong form counts as not set, and is no
  error on each refresh. A filter on a column of lists or records says that such a column can't be
  filtered. A value with more decimal places than an exact decimal column has is left out: `1.555`
  found the rows with `1.56`. A filter on a column of binary values can be saved, and **Add as
  step** keeps the same rows. A table shows 100 filters at most: more from a project file apply
  and stay in the file, and a line under the cards counts them.
- A project that can't be saved says so, and the window stays open with your work.
- A project file with data nested too deeply is refused when it opens, as a damaged file. A map
  position that is not a finite number counts as not set.
- A project file written by hand with thousands of steps without an id, or a formula of thousands of
  lines, no longer blocks the window for minutes. A step with more than 1,000 rows in a list shows
  their number in place of the rows, and keeps them. The settings of a step with thousands of columns
  or tables fill faster, and the colors of a formula with emoji no longer slow down.
- A `PIVOT` in a formula needs its values (`ON [Column] IN (...)`), and says so.
- The report after a program that ended without a message keeps each name on one line, and it says
  when a step asked for more memory at once than was free.
- Plugins: `ctx.database` has a keyword `read_only` (a connector can override `connect_read_only`;
  one that doesn't gets a normal connection, so plugins work as before). `ctx.check_memory` is
  new for plugins that read in batches. `Database.read`, `Database.read_table` and
  `frame_from_cursor` take an optional `between` (a `Database` subclass that overrides `read` or
  `read_table` must take it too), and `Database.run_script` runs a command with the error
  handling of the other methods. `tafl.api.xlsx` has `write_frame`, `cell_text` and
  `escaped`, for writing text as text.
- The user guide: a warning at the CSV export (a file for people goes to Excel, CSV goes to
  programs), a known limit in chapter 10 (a small, unusual file can end the program, and an unsaved
  sheet is then lost), and features it didn't explain yet: the search and sort of the filter pane,
  the menus of the map, of a group in the Tables panel and of a sheet cell, `tafl --version`, the
  exit codes of `tafl show`, and more.

## 0.6.0 – 2026-10-31

The data sheet: a simple way to bring data that is not in a file into a project. Chapter 10 of the
user guide, "Data sheets", describes it; the chapters after it moved up by one number.

- **Data sheet** (**+ New table → Paste from clipboard** or **Empty sheet**, also on the map):
  copy data anywhere with Ctrl+C, in a spreadsheet program, an ERP system, a web page or a mail, and paste
  it into a sheet. A sheet is a window of its own, so the map stays in view beside it. It has rows
  and named columns; the first row can be the column names. You can type into cells, paste more
  data, fill a selection with a value, and add and delete rows and columns, with an undo of its
  own. It is no spreadsheet program: no formulas, no cell formats, no calculations. Those are
  steps of the table.
- Pasting reads the plain text of the clipboard, which is the same for every program: at tabs
  first, otherwise at the separator that makes the biggest table, also with a title, a greeting or
  a closing line around it. Quoted cells with line breaks, empty rows and cells at the end, and a
  single column of values are fine. A list with decimal commas (`1,50`, `12,5 %`) stays one
  column. A list in a frame of `|`, as in a list from an ERP system or a Markdown table, loses the frame and its
  ruler lines. When the text was split at semicolons, commas or `|`, **Paste as one column** takes
  the lines as they are.
- **Add as table** asks for the file name, the format (`.xlsx` by default; CSV, TXT or Parquet)
  and the folder, writes the file with the old file kept until the new one is complete, and adds
  a normal table with one import of that file. It works on the map, in groups, merges, runs and
  the Find panel like every other table. The data stays in that file: none of it goes into the
  project file or the undo of the project. Undo removes the table; the file stays. Rows
  at the end without a value, and columns at the right with no value and no name of their own,
  stay out of the file.
- **Pasted data stays text**: every column is text, exactly as pasted (`00123`, `1,50`,
  `27.02.2026`, `1E5`), and the import step reads the file back exactly as the sheet shows it.
  Tafl doesn't guess: *Change type / format* converts a column.
- **Edit in sheet**, a button on the import step of a table made by a sheet, opens the file in a
  sheet: the data at the first step, never the result of later steps. **Save** writes the file
  back, and the tables that read it are calculated again. Other files have no button: an edit
  could lose their cell formats and formulas. When the file was changed or removed outside Tafl,
  Tafl says so before it opens or overwrites the file.
- A new sheet is a draft: Tafl asks before it closes the sheet or the app. Its data is only in
  that window, so a crash loses it.
- A sheet holds at most 40,000,000 cells and 16,384 columns. 1,000,000 rows of 20 columns work;
  written as `.xlsx`, they take about 2 minutes, and about 2.6 GB of memory to read, and the dialog
  says so. CSV and Parquet take seconds.
- **Import Excel files**: a step made by a sheet keeps cells of only spaces and rows without a
  value, and every column name, so that it shows exactly what the sheet shows. All other imports
  read as before.
- **Import Parquet files**: a `[` in a file or folder name is part of the name. It was read as a
  pattern, so the import failed, or read a file with a similar name.
- Projects with such tables open in 0.5.0 as well: the table is a normal table without the button,
  and only an `.xlsx` file, or a Parquet file with `[` in its path, reads differently there (the
  cases above).
- Paths: a file in a folder whose name starts with `~`, or with a `%` in its name, below the folder
  of the project, stays valid after Save as. The `~` was read as the home folder there, and with
  a `%` the import failed, or a run wrote to the same place below the new folder.
- Closing: when the run ended while the question about closing was open, **Wait** did not close
  the window, and the next run did.
- Plugins: `tafl.api.files` has `detect_delimiter` and `count_outside_quotes` (they moved there
  from the CSV import), `ONE_COLUMN`, `SHEET_KEY`, `sheet_marked` and `in_folder`.
- Memory: Tafl gives the memory of tables that are gone back to Windows at once, instead of
  keeping it for later use. Very big tables can take a little longer to read.
- Undo and Redo also calculate the tables they changed that are not selected, and the tables that
  use them: such a table could show an old status until it was selected.
- **Pivot** needs far less memory with many new columns and few rows in the result: 1,000 new
  columns over 12 row keys took about 5.6 GB on a PC with 16 threads, and could stop the app on a
  PC short of memory. The new columns are now made 32 at a time, and the result is the same.
- `tafl run` needs less memory with many tables: the data of a table goes as soon as no later
  table uses it. What a table imported stays until the end of the run, for the tables that import
  the same. A test project of 14 tables with 1,000,000 rows each needs 1.6 GB instead of 2.6 GB.
- When memory runs out, Tafl says so:
  - A step doesn't start with less than 0.5 GB of memory free, even after Tafl has let go of its
    stored results. It fails with a normal error that says how much is free, and calculates again
    like any other step.
  - On Windows, `tafl run` does its work in a second process. If that ends without an error message, the run says at
    which step it stopped, on the screen and in `%LOCALAPPDATA%\Tafl\tafl.log`, and ends with the
    new exit code 3.
    In the Task Manager a run now shows two `tafl.exe`.
  - If the window closes without an error message during a calculation, the next start says at which step.

## 0.5.0 – 2026-10-28

Four new steps: Check data, Pivot, Unpivot and Split column. The user guide describes them in
chapter 4, "Actions".

- **Check data** (under Rows): checks the number of rows at this step (at least, at most,
  exactly or between two numbers) and, when a check fails, either stops the table or only warns.
  *Stop the table* is an error like any other: the steps after it, with the table's exports and
  commands, don't run, the tables that use it get the error too, and a run fails (exit code 1).
  *Only warn* shows a warning on the step and in Run results, and the data goes on. The data
  itself never changes. A new step starts with "at least 1 row" and *Stop the table*.
- **Pivot** (under Columns): turns the values of one column into columns, like an Excel pivot
  table without totals: one row for each combination of the row keys, one column for each value,
  and in each cell the Sum (the default), Count, Average, Minimum, Maximum, First or Last value of
  the rows that meet there. Cells without rows stay empty. The new columns are sorted or come in
  the order the values first appear, all empty values go into one column "(empty)", and there are
  at most 1,000 of them (and at most 50,000,000 cells).
- **Unpivot** (under Columns): turns columns into rows, the opposite of Pivot. The chosen
  columns, or all columns except the chosen ones, become a column with their names and a column
  with their values; empty values can be left out. Whole and decimal numbers become decimal
  numbers, whole numbers and exact decimals exact decimals, and dates and date-times date-times,
  when every value stays as it is; other mixes, also decimal numbers with exact decimals, become
  text, and the step says so, so that no value is changed without notice. The result has at most
  250,000,000 cells (rows × columns).
- **Split column** (under Text): splits text at a separator (comma, semicolon, space, tab, line
  break or any other text) into new columns ("Müller, Hans" into Last name and First name; the
  last column gets the rest of the text as it is) or into rows ("A;B;C" into three rows, with the
  other columns repeated).
- Projects with these steps need this version: 0.4.0 shows them as unknown actions.
- **Run results** show warnings without a click: a step with a warning is orange and open, the
  table says "ok, 1 warning", the first line counts the warnings, and the status bar says "Run
  finished with warnings". A double-click on a line goes to its table or step and no longer
  opens or closes the line (the arrow in front of it does).
- **Aggregate**: a *Sum* of whole numbers beyond 9,223,372,036,854,775,807, or of durations
  beyond 292 years (106,751 days; Parquet files often keep durations in nanoseconds), stops the
  step with a message instead of giving a wrong number, and *Minimum* and *Maximum* of lists and
  records stop with a message instead of giving empty values. *Pivot* does the same.
- The list of steps writes wide tables as "10,000 × 1,001".
- In a list of settings, a field that no row shows has no column heading (e.g. *and* of Check
  data's *between*), and number fields are as wide as their number, without arrows (the mouse
  wheel and the Up and Down keys still change them), so that a row fits a narrow settings panel.
- Plugins: `tafl.api.dtypes.text_expr(expr, dtype)` writes the values of any column as text that
  is the same on every PC (as Pivot names its columns): 2 and not 2.0, every digit that tells a
  decimal number apart and no exponent, dates as 2025-12-31.

## 0.4.0 – 2026-10-25

A Find panel that lists what a project reads and writes and finds any step by its texts, marks
for imports and exports on the project map with a highlight of what a table uses and what uses
it, and a merge that says how many rows matched.

- **Find panel** (**Find** in the toolbar, Ctrl+Shift+F, **View → Find panel**): a panel beside
  the tables view and the map. With an empty search it lists every import, export and command,
  each with the file, folder or sheet, or the connection and database table, that it reads or
  writes. A search finds steps by the names of their table, group and action, their summaries
  and notes, and every text setting: files, SQL, formulas, conditions, column names, connections
  and parameters; all words must match. Filters show the steps with problems, those turned off,
  those changed since the project was opened, and those that use a parameter. The results can be
  grouped by kind, by table, or by place (folder or connection, then file or database table). A
  click selects the step in the current view; Enter in the results or a double-click also puts
  the cursor into the setting that matched. Enter in the search field goes to the next result,
  and F3 and Shift+F3 go to the next and previous result from anywhere.
- Project map: the rows of imports are marked **in**, those of exports and commands **out**, and
  a collapsed group counts them ("3 in · 2 out").
- Project map: H (or **Uses / used by** above the map, or a box's right-click menu) shows the
  tables the selected table uses, directly or through others, and the tables that use it, each
  in a color of its own; the other tables dim. H or Esc ends it.
- Merge with another table says how many rows matched, with up to three keys of the rows without
  a match: "152 of 156 rows matched, 4 without a match: 09999". Each merge type says it in its
  own words, e.g. how many rows an inner join left out.
- The box at the top of the step settings shows what a step reported, such as the merge's
  message or what an import detected. Before, these messages were only in Run results.
- Plugins: `endpoints=` in `action(...)` tells the Find panel what a step reads and writes
  (`Endpoint`, `file_endpoint` and `source_endpoints` in `tafl.api`). Without it, the action's
  file, folder and connection settings count, and an import built with `source_params` gets the
  endpoints of `source_endpoints`, so plugins keep working as they are.
- Fixes: on the project map, a step turned on or off shows it at once (a dashed arrow, a pale
  row), and a new summary is on its row at once. What is typed into a setting stays with its
  step when another step is selected before the field is left. Undo and Redo in the toolbar or
  the Edit menu apply what is typed into a setting or a note first, as Save and Run do, and Undo
  takes back just that typing; before, the setting's values were written into the step selected
  by the undo, and Redo was lost. A merge with the same row in
  **Match rows where** twice, or with one column of the other table for two columns of this
  table, works instead of stopping with an error. So does a full join of a date, date-time, time
  or true/false key with a text key, and a merge on two time spans of different units; keys that
  can't be compared at all, such as a duration and a text, stop with a message that says so and
  what to do. A time compared with a text keeps its fractions of a second: 08:00:00.5 no longer
  matches "08:00:00". A column added by a merge no longer stops the step when its new name
  ("City (2)") is the name of the other table's key column: it gets the next one. A full join that uses only the first matching row adds
  every row of the other table without a match, also those with an empty or a repeated key. Undo
  of a connection rename brings back the old name in the steps that use the connection. When the
  key columns of a database export don't identify the rows, the message names the same key value
  every time: the first one in the order of the rows that occurs more than once, and a key column
  may be named "len". Undo after saving
  the project in another folder brings back file paths that still point to the same files.

## 0.3.1 – 2026-10-23

- A user guide with pictures of the window: [docs/guide.md](https://github.com/xv8mna/tafl/blob/main/docs/guide.md),
  `Guide.html` in the download and **Help → User guide**.
- On the project map, Esc in the search field ends the search, as it does on the map.
- The help of "When there are no files" says which columns an empty table has.

## 0.3.0 – 2026-10-21

Options for days without data: a folder import can go on without files, a new step skips the
rest of a table when it is empty, and every export can leave out an empty table. Also many
fixes, among them safer exports and file commands, SQL Server updates and upserts that store
every value as it is, and text sorted as in Excel.

Old projects mostly work as before, and the new options start with the old behaviour. But some
fixes give other results, and a step of an old project can then fail, e.g. a formula that took
amounts in a CSV file for text: "Results that change" lists these changes. Once a project is
saved by 0.3.0, 0.2.0 can't calculate its file imports, file exports, database exports and the
commands *Copy*, *Move* and *Delete files* any more (they say they were saved by a newer
version), nor the new step *Skip the rest if a table is empty* (an unknown action there).

### Days without data

- Folder imports (CSV, Excel, Parquet) have the choice **When there are no files**: stop with
  an error (as before), continue with an empty table (with a warning; it has only the File and
  Path columns), or skip the rest of the table. A folder that doesn't exist is still an error.
- New step **Skip the rest if a table is empty** (under Rows): when this table, or another one,
  has no rows, the steps after it are skipped, e.g. the exports. That is not an error: the run
  succeeds, and the table's result is the data at this step.
- A step that uses a table which skipped its rest and has no rows (*Start from*, *Merge*,
  *Append*, …) doesn't run, and the rest of its table is skipped too: tables that start from an
  import on a day without files skip their exports as well.
- Every export (CSV, Excel, database) has the option **Don't export an empty table** (off by
  default): the file or the database table is left as it is, and the step says "Not exported:
  the table is empty".
- Skipped steps show as *skipped* with the reason: in the steps, settings and data panels, in
  Run results ("ok, rest skipped: …"), on the map (dimmed, and "rest skipped" on the box) and on
  the command line.
- The README's daily-exports example uses them: on a day without files, the exports and the
  Move files step are skipped, and the table has no error, also right after a run that emptied
  the inbox. Before, the table showed an error after every run, and a run without files failed.
  Its Move files step takes all files the import read (**Which files**, see File commands), also
  those without rows.

### Exports

- CSV and Excel exports, also "Replace only this sheet", write the new file next to the old one
  and replace it only when it is complete. Before, a failed export, a run that was ended or two
  runs at once could leave a broken or half-written file. The next export of a file deletes the
  `~$…` file that a run on the same computer left when it was ended in the Task Manager (and a
  later run its folders in `%TEMP%`).
  In a folder where no new files can be created, the file is written over directly, with a
  warning. A hidden file stays hidden.
- Plain messages when the file is open in another program, is read-only, or the folder can't be
  written or created, and for names that can't be file names (`con.csv`, `?`, more than 255
  characters). Before, some of these were raw system errors, or named the wrong cause.
- Binary values (e.g. from `varbinary` or SQLite BLOB columns) are written as hex text,
  `0x0102FF`. Before, the CSV and Excel exports failed.
- CSV: decimal numbers are written with at most 15 significant digits, as Excel shows and saves
  them (0.37 * 3 gives 1,11, not 1,1099999999999999); whole numbers and exact decimals keep all
  their digits. Infinite and not-a-number values are written as empty, as in Excel; times as
  hh:mm:ss. A table with rows but no columns gives an empty file (it stopped with a raw error).
- Excel: texts longer than 32,767 characters are cut with a warning; a table with more rows or
  columns than a sheet holds stops with a plain message. Decimal columns get the number format.
  A table without columns (e.g. a folder import without files and without its File column) gives
  an empty sheet, as the CSV export gives an empty file; before, it stopped the export.
- Excel: the help of "If the file exists" describes both choices. A sheet name can't start or
  end with `'` (Excel doesn't allow it), and the log names the sheet as the workbook has it.
- The logs say "Wrote 1 row".

### Databases

- SQL Server: *Update*, *Upsert*, and *Replace* or *Delete the rows with the same key values*
  store values as *Add the rows* does, and keys compare as the table's own columns do (their
  types and collations). Before, texts of more than 255 characters failed, fractions of a second
  were cut (date-time keys could hit the wrong rows), and characters outside the Windows code
  page became "?" (keys like 'Łódź' found no row). Keys of every type find the rows that *Add*
  stored, e.g. `datetime`, `datetime2(0)`, `time`, `varchar` and `varbinary` keys and long
  texts.
- SQL Server: values are stored as their columns hold them, in every mode: fractions of a second
  are cut to the column's digits (*Add* into `time(n)` and `date` columns works now), decimals
  are rounded to the column's decimal places as SQL Server rounds them (1.005 is 1.01) and keep
  all 38 digits (more than 28 were rounded), and spaces at the end of a text that don't fit are
  cut. Texts for `char` and `varchar` columns get the characters of the column's collation, also
  with *Add*: Łódź stays Łódź in a Polish or UTF-8 column (it became Lódz). *Update* and
  *Upsert* refuse keys that are equal as the column holds them (08:00:01.2 and 08:00:01.7 in a
  `datetime2(0)`).
- SQL Server: date-times with a time zone are written in that zone's local time, without the
  zone, as SQLite and the Excel export do. Before, they were written in UTC. A `datetimeoffset`
  column gets the zone's offset with them, and the offset of this PC's time zone for a date-time
  without a zone, in every mode (*Update* and *Upsert* wrote +00:00).
- SQL Server imports keep the digits of `numeric` and `decimal` columns: whole numbers of 19 or
  more digits and decimals of more than 15 digits were decimal numbers, so different IDs could
  become equal.
- SQLite writes each date-time the same way in every export, whole seconds without `.000000`.
  Before, the other values of the export decided, so keys could miss their rows.
- SQLite exports exact decimals (e.g. from Parquet or SQL Server): a number column gets them as
  numbers, a text column with all their digits; new tables get `INTEGER`, `REAL` or `TEXT`,
  whichever keeps their digits. Before, such an export failed.
- SQL Server: Windows' own driver "SQL Server" is no longer chosen by itself, as it reads dates
  and times as text. Without ODBC Driver 18 or 17, the message says what to install; the old
  driver still works when it is entered in the connection. A driver that isn't installed gets
  a list of the ones that are.
- SQL Server: importing a whole table reads `sql_variant`, `geography`, `geometry` and
  `hierarchyid` columns as text. In a query, the message names the column and shows the `CAST`
  to write. Before, the import failed with a driver message.
- SQL Server error messages are shorter: each message once, without the driver's prefixes and
  error numbers. The same error for many rows (e.g. duplicate keys) is shown once, with how many
  more there are, and it keeps the duplicate key value.
- Export to database: "Created table" shows only when the export worked. SQL Server: a `#temp`
  table as the target is refused (it would disappear); an infinite number is refused with a
  hint; a text too long for its column names the column and an example; new tables get
  `DECIMAL(p, s)` for decimal columns, and text columns count an emoji as two characters, as
  SQL Server does.
- Run SQL command: `GO 3` runs the batch three times, and a comment may follow `GO`. The log
  says "1 row changed".
- SQLite: an import's `ATTACH` works only when the project is run (while a project is only
  looked at, it could create a file). `BEGIN`, `COMMIT` and `ROLLBACK` in a command are refused
  with a plain message: the command runs in one transaction.

### Imports

- CSV: values like 1.000 or 12.000 (thousands with a point) are no longer read as decimals,
  which also left real decimal-comma columns as text. They follow the other numbers of their
  column (0.500 or 2,5 shows the separator), else those of the files; without any, they are
  thousands in files with semicolons. The log names the decimal separator of the files and
  the columns that have the other one.
- CSV: a file whose first line has no delimiter is one column, so values like 1.234,50 keep
  their comma (they were split at it); line ends converted twice (`\r\r\n`) count as one.
- CSV: amounts like 1.234,50 €, € 5 or 123,45- and percentages like 12,5% (read as 0.125)
  become numbers when every value of the column is one. Spaces between digits count only there,
  as thousands separators (1 234,50 €): phone numbers, card numbers or 2025 01 31 stay text.
- CSV: warnings for characters that don't fit the chosen encoding, and for rows with more
  values than the first row (the extra values are left out). With "Detect automatically", a
  UTF-8 file with a few wrong bytes is read as UTF-8, with a warning.
- "Detect numbers and dates" (CSV and database imports) keeps whole numbers of up to 19 digits
  exactly, also with thousands separators; a column with longer ones stays text, with a warning.
  Before, 19-digit IDs became decimal numbers, so different IDs could become equal.
- Excel: cells with only a time become times; a header row below the data gives a warning; the
  line fastexcel printed on the console is gone. The help explains merged cells and formulas
  without saved results.
- Parquet: decimals without decimal places become whole numbers when every value fits; other
  decimals keep all their digits, as before. Files with decimals of more than 38 digits can be
  read (before, they couldn't): values too long to be kept exactly become text, with a warning.
- CSV, Excel and Parquet: a file that another program has open without letting others read it
  says so ("… is open in another program"). Before, it was a raw system error.
- CSV with the decimal separator *Comma* chosen: 0.500 stays text (it was 500); thousands
  separators come after 1 to 3 digits that don't start with 0, in *Change type* too.

### Steps

- *Sort* orders text as Excel does: upper and lower case count the same, ä, é … sort with their
  base letter, ß like ss; equal values keep their order. *Number rows* uses the same order.
  Before, capitals came before small letters, and umlauts after z.
- Dates with two-digit years are read as in Excel: 00–29 are 2000–2029, 30–99 are 1930–1999,
  also with a time (31.12.25 08:30). Before, *Change type* without a format, and typed values in
  *Filter rows*, *Replace* and *Fill*, read 31.12.25 as the year 25.
- *Change type* → Whole number keeps all digits (before, values beyond 2^53 changed, and the
  largest became empty); → Decimal number says which values were rounded; Binary → Text gives
  hex, `0x0102FF`.
- *Change type*, numbers → text: formats with `%`, with text before or after the digits
  (`#,##0.00 €`, `"No. "0`) and with sections for positive and negative numbers and zero.
  Formats it can't write (e.g. `0.00E+00`) stop with a message instead of giving wrong text.
  Month and day names are English, as the help now says.
- Columns of exact decimals (e.g. from Parquet): *Filter rows* and *Replace* with typed values
  and *Fill* keep every digit, *Change type* → Text writes them all, and → Decimal number says
  which values were rounded. The grid shows them with all their digits and the PC's decimal
  separator.
- *Merge*: keys of different types are compared without changing this table's key column. Only
  a full join whose other table has keys that don't fit its type (2.5 for whole numbers) gives
  it a type that fits both. Whole numbers and exact decimals of different types are compared
  with all their digits.
- *Replace*: an invalid regular expression says why. *Aggregate*: "Sum needs a number column;
  Status is text"; average and median also take dates.
- `UPPER` in formulas writes ß as SS, like *Clean text* (GRÖSSE, not GRÖẞE).

### File commands

- *Copy*, *Move* and *Delete files* have the new setting **Which files**: *Listed in a column*
  (as before), or *All files this table's import read*: every file the table's folder import
  read, also files with only a header row or whose rows a filter left out (of Excel workbooks,
  those of which it read a sheet). In a table without a folder import, the step shows a warning
  while the project is edited, and a run stops it with a message.
- A file command that is stopped part-way (Stop, or Ctrl+C on the command line) says how many
  files it had copied, moved, renamed or deleted, in Run results and on the command line.
- *Copy*, *Move*, *Rename* and *Delete files* check all files before they change the first
  one: a missing file, a name that Windows doesn't allow (`..`, `:`, `CON`, more than 255
  characters, …), a read-only file to replace or to delete for good, or a folder where a file
  belongs stops the step with nothing changed. When Windows fails part-way, the message says
  how many files were done.
- Each file is copied or moved completely or not at all, also when the run is stopped: a move to
  another drive deletes the file only when its copy has the new name, and a file it replaces is
  kept until then. Before, a file open in another program could leave a copy behind, and
  *Replace it* could lose the old file. Only *Copy files* with *Replace it*, in a folder where no
  new files can be created, writes over the old file directly, with a warning, as exports do.
  Messages name the file that is open in another program or read-only, or the folder where no
  files can be created.
- Two listed files that would get the same name (e.g. `b.csv` from two date folders, moved
  into one folder): *Keep both* numbers them, *Replace it* and *Stop with an error* stop the
  step and name both. Before, *Replace it* deleted the file moved a moment before.
- *Replace it* never replaces a file the step has just written, or one of its own files.
- *Rename*: a file that already has its new name stays as it is (before, it became
  `name (2)` or stopped the run); a new name that differs only in upper or lower case renames
  the file.
- Plain messages for copying into a "folder" that is a file, and for *Create folder* and
  *Delete files* when a file or folder is in the way.

### The app

- A step just added is *incomplete* until its settings are filled in (grey-blue), not an error:
  the table isn't red, and the data panel shows the data going into the step. A run still
  counts it as an error. Its summary shows `?` for what is missing ("Keep rows where ?").
  Tables that use its table are *waiting* (grey-blue), not red.
- The status of the other tables follows changes, also Undo: tables that use a changed table
  are calculated again in the background, and **Reload data** calculates every table again.
  Before, a table could stay red until it was selected.
- Long texts above the data (a step's summary, a skip reason with a path) end with "…" and
  never make the window wider; the window can be made much narrower.
- **Stop** says "Stopping after the current step…", and a stopped run says so in the status bar
  and in Run results, which list the tables and steps that ran before the stop. A formula that
  is running stops at once (before, Stop waited for it).
  Closing the window during a run asks whether to wait or to stop.
- Ctrl+A and Ctrl+C on a big grid no longer freeze the window: up to 2,000,000 cells are
  copied (many in the background), more are refused with a message.
- *Aggregate* and other settings with lists fit a narrow settings panel; the panel scrolls
  sideways when it has to.
- Map: Ctrl+F, Ctrl+G and the group keys 0–9 work anywhere on the map; ◀ ▶ appear when not all
  group chips fit.
- Saving over a project file that was changed since it was opened, e.g. in a second window,
  asks first. Saving into a folder without permission, or over a read-only project file, gives a
  plain message, and the file paths of the steps stay as they were.
- Filter pane: **All**, then **Apply**, on many values no longer freezes, and the project no
  longer stores every value: a big selection is stored as the values left out. The value lists
  sort text in the order of *Sort*.
- The Parameters dialog works out formulas in the background: a slow formula doesn't block it.
- A connection test's error shows below the button, in full, and can be copied. A test's result
  stays with its connection when another one is selected meanwhile.
- A table can't get an empty name.
- Higher contrast for hints, placeholders, the map's step rows and colored texts (e.g. in Run
  results); the first window fits the screen.
- Run results show how long the run took. Before, they added up the time of the steps that
  worked, so a run whose slow step failed took "0.00 s". A table that is turned off is no error
  in the status bar.
- A step whose result was kept from before shows its messages again, e.g. the warnings of an
  import. Before, they showed only when it was calculated.
- "1 row" and "1 column" in the data panel, the tables list and Run results.
- Help → Plugins and the status bar warn about folders in `TAFL_PLUGINS` that don't exist.

### Command line

- Ctrl+C stops a run, also during a formula, at once: "Error: Stopped", exit code 130. Each
  table is printed as soon as it is calculated, and after Ctrl+C also the steps of the stopped
  table, so the output shows what ran. Each line reaches a log file at once, also when the run is
  ended; a step's summary is cut after 300 characters, as in the window. A Ctrl+C that comes
  while the run ends no longer makes `tafl.exe` hang.
- `show` says why a table is skipped, and exits with 0 for a table that is turned off.
- Plain messages for a project file that isn't there, a folder, or a file that can't be read.
- A parameter formula that fails because of a `--set` value of another type says so.
- `--rows` must be at least 1, and a table of one row says "1 row". Every argument has a help
  text, and `--help` lists the exit codes.
- Warnings for plugin folders given with `--plugins` or in `TAFL_PLUGINS` that don't exist.
- Without Unicode, tables with symbols in their data line up.

### Parameters

- The message for a name that can't be used gives the rule: letters A–Z, digits, _ and spaces,
  starting with a letter or _. On the command line, such a name that nothing uses only gets a
  warning.

### Results that change

Where an import now gives numbers or dates that later steps of an old project took for text,
change those steps, or switch off **Detect numbers and dates** in the import (then every column
is text).

- CSV with **Detect numbers and dates**: values with only thousands separators (1.000, 2.500)
  are 1000 and 2500, not 1 and 2.5. Amounts (1.234,50 €, 123,45-) and percentages (12,5% is
  0.125) are numbers, not text, so e.g. `REPLACE([Price], ' €', '')` or a filter for `5%` stops
  with an error. A column whose values show a decimal point is read with one also in a file with
  decimal commas: times like 08.30 become 8.3 (choose the decimal separator *Comma* to keep them
  text).
- CSV: a file without a delimiter in its first line is one column (values like 1.234,50 were
  split at the comma). With *Detect automatically*, a UTF-8 file with a few wrong bytes is read
  as UTF-8, not as Windows (ANSI). Line ends converted twice (`\r\r\n`) no longer leave an
  invisible character (`\r`) at the end of the last column's values.
- **Detect numbers and dates** (CSV and database imports): whole numbers of up to 19 digits are
  exact whole numbers, longer ones stay text with a warning; both were decimal numbers, so a
  formula like `[Id] + 1` on longer ones stops now. Date-times with two-digit years
  (31.12.25 08:30) are date-times, not text.
- Two-digit years 30 to 69 are 1930 to 1969: the detection of the imports and formats such as
  dd.mm.yy made them 2030 to 2069. *Change type* without a format, and typed values in *Filter
  rows*, *Replace* and *Fill*, read 31.12.25 as 2025, not as the year 25.
- Excel: cells with only a time are times, not date-times on 31.12.1899, so formulas such as
  `STRFTIME` and filters on them need a change.
- Parquet: decimals without decimal places are whole numbers when every value fits; smaller
  number types (e.g. Int32, Float32) become the usual whole and decimal numbers, so formulas on
  them calculate with more digits; categories become text.
- SQL Server imports: `numeric` and `decimal` columns keep their digits (see Databases), as
  whole numbers or exact decimals instead of decimal numbers.
- *Sort* and *Number rows* order text differently (see above).
- *Change type* → Whole number keeps all digits. Numbers → text writes the text and `%` of
  number formats, `General` and `@` write the number as it is (1,5, not 2), a comma after the
  digits divides by 1000 (`0.000,` writes 1234,5 as 1,235), and formats it can't write stop the
  step.
- *Change type* to a date or date-time without a format reads each text in its own format, so a
  column with 2025-12-31 and 31.12.2025, or with dates next to date-times, gets no empty values
  (a date alone is that day at 00:00). Before, only the format of most values was read.
- *Aggregate*: *Sum* of a date or date-time column, and *Average* or *Median* of a text column,
  stop the step with a message (they gave empty values).
- `UPPER` writes ß as SS.
- *Merge* keeps the type of this table's key columns, except in a full join whose other table's
  keys don't fit it.
- CSV exports write decimal numbers with at most 15 significant digits (1,11, not
  1,1099999999999999), infinite and not-a-number values as empty (they were inf and NaN), and
  times as hh:mm:ss (they were 08:30:00.000000000).
- SQL Server: a connection without a driver of its own no longer falls back to Windows' driver
  "SQL Server" (enter it in the connection to keep it). Exports write date-times with a time
  zone in that zone's local time, not in UTC.
- SQL Server exports by key store and find values as *Add the rows* does, and *Update* and
  *Upsert* refuse keys that are equal as their column holds them; `datetimeoffset` columns get
  the zone's offset, or this PC's, in every mode, and `char` and `varchar` columns the
  characters of their collation, also with *Add* (see Databases).
- SQLite: exports write whole seconds without `.000000`, also when other values have fractions
  (a key no longer finds a row written before as 08:00:00.000000); *Run SQL command* refuses
  `BEGIN`, `COMMIT` and `ROLLBACK`.
- *Rename files* leaves files that already have their new name; *Move* and *Copy files* with
  *Replace it* stop when two listed files would get the same name.

### Other

- A plugin that calls `sys.exit()` (or raises SystemExit or KeyboardInterrupt) no longer ends
  the app or the command line: it is a load error, or an error of its step (also from its
  `migrate`, `summary` or `fingerprint` function).
- An import that can't tell whether its data changed is read again for every calculation, also
  on Python 3.12, whose clock could make two quick calculations share an old result.
- Big tables: the results kept for the previews take at most a quarter of the installed memory
  (it was a fixed 4 GB), and only the last two settings of each step are kept, so memory no
  longer grows with every edit.
- The download's `README.txt` explains how to open a project, what removing the folder leaves,
  and a batch file for scheduled runs with a log.

### For plugins

- `SkipRest(message, df=None)` in `tafl.api`: ends the table at this step without an error.
- `source_params` has the new setting `no_files` (its default keeps the old behaviour);
  `source_files` can then give no files, and `combine([])` gives a table with only the File and
  Path columns. For a folder, `source_files` notes the files and the folder in
  `ctx.imported_files` and `ctx.imported_folder`, which the later steps of the table get.
- `@action(..., checks=)`: a function `(params, vc)` that gives the problems the parameters
  can't see on their own, e.g. with `ValidationContext.imported` (whether the table's import read
  files from a folder). They stop the step before it runs; for exports and commands they show as
  warnings while the project is edited.
- `TableResult.stopped_messages`: the messages of the step that was running when the run was
  stopped.
- `files.replace_file(target, warn)` writes a file safely; `copy_file`, `move_file` and
  `delete_file` change a file completely or not at all (a step that changes many files does it
  in `with files.batch():`); `name_problem` and `check_folder` check names and folders first;
  `files.read_error` gives a plain message for a file that can't be read.
- `dtypes.parse_integers`, `parse_decimal`, `float_loses_digits`, `text_sort_key` (for an
  expression or a Series) and `text_key`; `normalize(..., exact=True)`;
  `infer_types(..., warn=)`.
- Connectors: `input_sizes(df, types, temporary)`, `stage_types`, `fit_frame` and
  `read_table_sql`, `Database.execute_many(..., sizes=)`; `column_types` may give lengths and
  digits (`nvarchar(50)`, `decimal(10,2)`, `datetime2(0)`; SQL Server: a UTF-8 `varchar` with
  its collation); `make_series` keeps the digits of
  decimals (Int64, Float64 or Decimal), and stops with the message of an `ActionError` value,
  with `{column}` standing for the column's name.
- `xlsx.replace_sheet` returns the name of the sheet it replaced, as the workbook has it, or
  None when it added the sheet (it returned True or False).
- A summary gets `?` for a required setting that is still empty.

## 0.2.0 – 2026-10-16

The first version with a Windows download: a folder with `tafl-app.exe` (the window) and
`tafl.exe` (the command line), no Python needed. Unzip it, start `tafl-app.exe`, and open
`examples\demo.tafl` to try it. New: parameters, values that the whole project uses, and help
with formulas (a list of the functions, a formula window, checks while typing).

### For plugins

- The Python package is called `tafl` now, like the app: plugins import from `tafl.api` instead
  of `etl_tool.api`, and the base class of `ActionError` and `Cancelled` is `TaflError` (it was
  `EtlError`). A plugin that still imports `etl_tool.api` doesn't load, and its error says what
  to change.
- New in `tafl.api`: `parameters` (the project parameters and how `{Name}` is filled in),
  `all_results` and `split_name`; `Connector.literal`, `sql_spans`, `fill_sql`, `missing_table`
  and `column_types`, which a connector can override for its database; `expressions.tokenize`,
  `column_names`, `is_panic` and `evaluate(..., condition=True)`; `P.Expression(creates=...)`
  for the formula of a new column; `accepts` of every setting (a value of the wrong kind);
  `dtypes.type_label`; a `warn` function for `xlsx.replace_sheet`.

### Updating an installation from source

```
git pull
.venv\Scripts\python -m pip install -r requirements.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python tools\create_shortcut.py
```

The first `pip install` brings the libraries to the tested versions (tzdata is new). The second
one writes the commands `tafl` and `tafl-app` again: until then they look for the package by
its earlier name and don't start. The last line renews the desktop shortcut, whose icon file
moved. If a folder `src\etl_tool` is left (with only Python's cache files in it), delete it.

### Parameters

- **Project → Parameters** (also in the toolbar): values the whole project uses, written as
  `{Name}` in the settings you type, of steps and of connections: file names, folders, texts,
  column names, formulas and SQL. A value is fixed (`2026-10-01`, `500`, `D:\Reports`) or a
  formula (`=TODAY() - 1`) that can use the parameters above it. The dialog shows each value as
  it is now, or what is wrong.
- Formulas are worked out when the project opens, when the parameters change, on **Reload
  data** and for every run; the previews in between keep the values. A formula may take at most
  5 seconds.
- In formulas a parameter is a value of its type (`[Date] = {Day}` compares dates, a negative
  number is `(-5)`), and its text inside quotes and column names (`[Sales {Month}]`). In SQL it
  is written for the database (`CAST('2026-10-15' AS date)` on SQL Server, `N'…'` texts), its
  text inside '…'; names and comments stay as they are (found where SQLite and SQL Server find
  them, so a value can't end or start one). In file names and texts a fixed value stays as it
  was written (`2.50`).
- In formulas and SQL a value never joins a word or a number next to it: `Sales_{Year}` becomes
  `Sales_ 2026`, not the table `Sales_2026`. In SQL `5-{Offset}` with -5 becomes `5- -5`.
- SQL Server: the arguments of `EXEC` get constants, as SQL Server needs there (also in
  `EXEC (…) AT` a linked server): a date as `'20261015'`, a date-time as
  `'2026-10-15T08:30:00.000'`, read the same in every language setting.
- A fixed date-time with a time zone (`2026-10-01 08:30+02:00`) is that moment in local time in
  formulas and SQL, as `NOW()` is.
- Inside '…' a value is only text, but a text that is itself run as SQL (`EXEC('…')`,
  `sp_executesql N'…'`) makes it code there: give such a value to the dynamic SQL as a
  parameter instead (the README shows how).
- A `{Name}` that is no parameter is an error in a formula (outside quotes and column names)
  and, in a project with parameters, a warning in other settings.
- Formula fields: `{` lists the parameters with their values (also inside column names),
  `{Name}` has its own color (red where it would be an error), and the line under the
  formula and the formula window use the values.
- `tafl run --set Name=VALUE` (and `tafl show`) gives a parameter another value for one run;
  the run lists the values it used.
- Renaming a parameter renames it in the steps, in the other parameters and in the
  connections.

### Formulas

- **Formula functions** (F1): every function with what it does, how it is written and an
  example, with a search; names from SQL Server and spreadsheets find what to use instead.
- **Formula window** (F2): a big editor with the columns, the functions and a preview of the
  result on the first 1,000 rows.
- **A line under each formula** shows what it gives, or what is wrong, while you type; the
  mistake is underlined and a misspelled name can be corrected with one click.
- **Suggestions while typing**: columns after `[`, functions and keywords after two letters.
- **Colors** for columns, functions, keywords, texts, numbers and comments; an unknown
  `[column]` is red, and the matching bracket is marked.
- Clearer messages: comments in formulas, a formula that ends too early, functions that don't
  exist (with what to write instead), columns that differ only in upper and lower case.

### Safer with project files from others

Opening a project calculates it. Formulas can only use the data of their step (no files,
network or settings), and imports roll back what their SQL query changed. See "Project files
from other people" in the README.

### Databases

- SQL Server: an error in a later statement of a SQL command fails the step, and nothing of the
  command is kept.
- Decimals that would be cut off in a whole-number column (1.5 → 1) are refused.
- Readable messages for values that don't fit their column, and for tables that can't be read
  (e.g. no permission).
- A query that fills a `#temp` table without `SET NOCOUNT ON` imports its result.
- SQL Server: a GO line inside a text or a comment of a SQL command no longer splits it into
  batches.
- SQL Server: when the ODBC driver can't read the SQL, the message says that it takes `{…}` for
  an ODBC escape (before, only "syntax error" in the language of Windows).
- Changing a connection's settings reads the data of its imports again (before, only
  **Reload data** did).

### Excel, "Replace only this sheet"

- A sheet with its own pivot tables, slicers or timelines is not replaced (move them to another
  sheet first); the step warns about other things the old sheet had of its own.
- On a sheet with only its header row, ranges down to row 2 follow the data.

### Other

- On Windows, a file or folder name with a `:` (e.g. a time, `08:30`) is an error; before, an
  export wrote the data into a hidden part of a file. So is a drive without a `\` after it
  (`C:data.csv`, a file in the current folder of drive C).
- A damaged project file shows a message that says what is wrong. Before, it gave an unexpected
  error, or closed the app when the app was started with it.
- A step setting of the wrong kind in a project file (e.g. a number where a table belongs) is an
  error of that step, with a message. Before, it gave unexpected errors.
- Project files that an editor saved with a byte order mark (BOM) open.
- Date-times with a time zone show in the grid and the filter pane on Windows: the time zones
  come from the tzdata package now.
- Tafl runs on Python 3.12 to 3.14. 3.15 doesn't work yet: DuckDB and pyodbc have no builds for
  it.
- The libraries' versions are pinned in `requirements.lock`; a weekly test runs the newest ones.
- The support for files and settings of an earlier name of the app was removed.

## 0.1.0 – 2026-10-15

The first public version.

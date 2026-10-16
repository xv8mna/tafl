# Checking "Replace only this sheet" in Excel

Tafl's Excel export can write one sheet into an existing workbook and keep everything else
(see "Exporting to Excel" in the README). Its tests build workbooks with a library and open the
results with LibreOffice; this list checks the same with Excel itself. It takes about ten
minutes. The data is the invented demo data.

## 1. Write the data sheet

1. Open `examples\excel-check.tafl` in Tafl (in the Windows download, or in a source install).
   Its table *Data* imports the demo orders, keeps those with a net amount above 500 (step 3)
   and writes them to sheet *Data* of `examples\output\excel-check.xlsx`, with "Replace only this
   sheet".
2. **Run project** (F5). The workbook now has the sheet *Data* with an Excel table named *Data*
   (columns OrderNo, CustomerNo, Date, Net, Status, 161 rows).

## 2. Build a report on it in Excel

Open `examples\output\excel-check.xlsx` in Excel and add a sheet *Report* with:

- a **PivotTable** from the table *Data* (Insert → PivotTable, source *Data*): CustomerNo as
  rows, Sum of Net as values;
- a **slicer** for that PivotTable (PivotTable Analyze → Insert Slicer → Status);
- a **chart** of the table's Date and Net columns (select them on the sheet *Data*, Insert → a
  line or column chart, then move the chart to *Report*);
- two **formulas**: `=SUM(Data[Net])` and `=COUNTA(Data[OrderNo])`.

Save the workbook as it is (Excel Workbook, .xlsx) and close Excel.

## 3. Write the data again, with other rows

1. In Tafl, change the limit in step 3 from 500 to 1000 (fewer rows), and **Run project** again.
2. Open the workbook in Excel and check:
   - Excel opens it **without** a message that it found a problem or repaired something;
   - the PivotTable and the slicer show the new data (Excel refreshes them when it opens the
     file), and the slicer still filters;
   - the chart covers the new rows, no more and no less;
   - the formulas show the new sum and count;
   - the sheet *Report* looks as before (formats, column widths).
3. Close Excel, set the limit to 0 (more rows than the first time), run again and check the
   same.

## 4. A sheet that can't be replaced

In Excel, add a PivotTable on the sheet *Data* itself, save and close. Run the project in Tafl:
the export step must fail with "The sheet 'Data' has pivot tables of its own …", and the
workbook must be unchanged. Remove that PivotTable again afterwards.

## Telling the result

Open an [issue](https://github.com/xv8mna/tafl/issues) with what you saw, the Tafl version
(**Help → About Tafl**) and the Excel version (File → Account → About Excel). If Excel repaired
the workbook, attach the workbook from before Excel repaired it and Excel's repair log (Excel
offers to show it; it is an XML file in the temp folder).

# PyInstaller: the Windows download, a folder with tafl-app.exe (the window) and tafl.exe (the
# command line). Built by tools\build.py, which also checks the result and zips it.
import importlib.util
import sys

from PyInstaller.utils.hooks import collect_submodules

# Both programs get the same modules, so that a plugin works in both: the built-in plugins, the
# standard library and Tafl's libraries, as in a source install. Left out of the standard
# library: other user interfaces (Tk, IDLE, curses), installing (ensurepip, venv), old tools,
# the texts of help() and antigravity (it opens a web browser). The tests are not in
# sys.stdlib_module_names, and the modules that Windows doesn't have are skipped.
LEFT_OUT = {
    "tkinter", "_tkinter", "turtle", "turtledemo", "idlelib", "curses", "lib2to3", "distutils", "ensurepip",
    "venv", "pydoc_data", "antigravity",
}  # fmt: skip
LIBRARIES = ["polars", "duckdb", "pyarrow", "fastexcel", "xlsxwriter", "lxml", "send2trash", "tzdata"]
# The optional parts of pyarrow that Tafl doesn't use: Flight, Substrait, ORC, remote file systems
ARROW_EXTRAS = {"flight", "_flight", "substrait", "_substrait", "orc", "_orc", "_dataset_orc", "_s3fs", "_gcsfs",
                "_hdfs", "_azurefs"}  # fmt: skip

# Files that Tafl doesn't use: static libraries, pyarrow's C++ and Cython sources and its tests,
# pyarrow's optional parts, Qt's translations and networking, the Qt plugins for styles (Tafl
# sets Fusion, which is built in), touch input, Direct2D and image formats (Tafl draws its
# icons), and Qt's software OpenGL (a widgets app doesn't use it)
UNUSED_SUFFIXES = (".lib", ".pyx", ".pxd", ".pxi", ".h", ".hpp", ".cc", ".cpp")
UNUSED_FOLDERS = (
    "pyarrow/include/", "pyarrow/includes/", "pyarrow/src/", "pyarrow/tests/", "pyside6/translations/",
    *(f"pyside6/plugins/{kind}/" for kind in ("tls", "networkinformation", "styles", "generic", "imageformats")),
)  # fmt: skip
UNUSED_FILES = {
    "arrow_flight.dll", "arrow_python_flight.dll", "arrow_substrait.dll", "arrow_s3.dll", "qt6network.dll",
    "qtnetwork.pyd", "qdirect2d.dll", "opengl32sw.dll",
}  # fmt: skip


def needed(entry):
    path = entry[0].replace("\\", "/").lower()
    name = path.rsplit("/", 1)[-1]
    return not (
        name.endswith(UNUSED_SUFFIXES)
        or path.startswith(UNUSED_FOLDERS)
        or name in UNUSED_FILES
        or (path.startswith("pyarrow/") and name.split(".")[0] in ARROW_EXTRAS)
    )


hidden = collect_submodules("etl_tool.plugins")  # found by listing their package when Tafl starts
hidden += collect_submodules("etl_tool.api")  # all of the plugin API, also its testing helpers
for name in sorted(sys.stdlib_module_names - LEFT_OUT):
    spec = importlib.util.find_spec(name)
    if spec is not None:
        hidden += collect_submodules(name) if spec.submodule_search_locations else [name]
for name in LIBRARIES:
    hidden += collect_submodules(name, filter=lambda module: "tests" not in module)
excludes = [
    *LEFT_OUT, *(f"pyarrow.{part}" for part in ARROW_EXTRAS), "PySide6.QtNetwork",
    "pytest", "IPython", "matplotlib", "pandas", "openpyxl", "uv",
]  # fmt: skip

programs = Analysis(["tafl_app.py", "tafl_cli.py"], pathex=["../src"], hiddenimports=hidden, excludes=excludes)
programs.binaries = [entry for entry in programs.binaries if needed(entry)]
programs.datas = [entry for entry in programs.datas if needed(entry)]
modules = PYZ(programs.pure)
icon = "../src/etl_tool/ui/app.ico"


def program(script, **options):
    """An EXE that runs one of the two scripts, with the shared modules. programs.scripts also
    holds PyInstaller's start-up code and runtime hooks, which both need."""
    scripts = [entry for entry in programs.scripts if entry[0] not in {"tafl_app", "tafl_cli"} - {script}]
    return EXE(modules, scripts, [], exclude_binaries=True, icon=icon, upx=False, **options)


window = program("tafl_app", name="tafl-app", console=False)
command_line = program("tafl_cli", name="tafl", console=True)
COLLECT(window, command_line, programs.binaries, programs.datas, name="Tafl", upx=False)

# PyInstaller: the Windows download, a folder with tafl-app.exe (the window) and tafl.exe (the
# command line). Built by tools\build.py, which also checks the result and zips it.
from PyInstaller.utils.hooks import collect_submodules

# The built-in plugins are found by listing their package when Tafl starts: name them here
hidden = collect_submodules("etl_tool.plugins")
excludes = ["tkinter", "pytest", "IPython", "matplotlib", "pandas", "openpyxl", "uv"]


def analysis(script):
    return Analysis([script], pathex=["../src"], hiddenimports=hidden, excludes=excludes, noarchive=False)


def needed(binaries):
    """Without Qt's software OpenGL (a widgets app doesn't use it) and static libraries (for
    compiling only)."""
    return [b for b in binaries if not b[0].lower().endswith((".lib", "opengl32sw.dll"))]


window, command_line = analysis("tafl_app.py"), analysis("tafl_cli.py")
window.binaries, command_line.binaries = needed(window.binaries), needed(command_line.binaries)
icon = "../src/etl_tool/ui/app.ico"
window_exe = EXE(
    PYZ(window.pure), window.scripts, [], exclude_binaries=True, name="tafl-app", icon=icon,
    console=False, upx=False,
)
command_line_exe = EXE(
    PYZ(command_line.pure), command_line.scripts, [], exclude_binaries=True, name="tafl", icon=icon,
    console=True, upx=False,
)
COLLECT(
    window_exe, window.binaries, window.datas,
    command_line_exe, command_line.binaries, command_line.datas,
    name="Tafl", upx=False,
)

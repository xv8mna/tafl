"""Write the lock files: the exact versions, with their hashes, that the tests run with and the
Windows download is built from. Run it after changing the dependencies in pyproject.toml, or with
--upgrade to take the newest versions pyproject.toml allows:

    .venv\\Scripts\\python -m pip install uv
    .venv\\Scripts\\python tools\\lock.py [--upgrade]

requirements.lock has what Tafl needs to run; requirements-dev.lock adds the tools for the tests,
requirements-build.lock the tools for the Windows download (tools/build.py).
All of them work for every Python version Tafl supports (3.12 and newer).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKS = {"requirements.lock": [], "requirements-dev.lock": ["--extra", "dev"], "requirements-build.lock": ["--extra", "build"]}


def main() -> None:
    upgrade = ["--upgrade"] if "--upgrade" in sys.argv[1:] else []
    for name, extras in LOCKS.items():
        command = [sys.executable, "-m", "uv", "pip", "compile", "pyproject.toml", *extras, "--universal",
                   "--python-version", "3.12", "--generate-hashes", "--quiet", *upgrade, "-o", name]  # fmt: skip
        subprocess.run(command, cwd=ROOT, check=True)
        print(f"wrote {name}")


if __name__ == "__main__":
    main()

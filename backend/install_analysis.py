"""Install the optional analysis dependencies only in the supported Python version.

Run: python3.11 -m venv .venv-analysis
     .venv-analysis/bin/python backend/install_analysis.py
"""
from pathlib import Path
import platform
import subprocess
import sys


def main() -> int:
    if sys.version_info[:2] != (3, 11):
        print(
            "Analysis installation requires Python 3.11. Basic Pitch 0.4.0's runtime "
            "constraints are incompatible with Python 3.12+. Python 3.10 is no longer "
            "recommended because it is end-of-life. Create a Python 3.11 virtual "
            "environment and rerun this script. See docs/DEPENDENCIES.md for the "
            "Apple Silicon compatibility caveat.",
            file=sys.stderr,
        )
        return 1
    if sys.prefix == sys.base_prefix:
        print("Create and activate a Python 3.11 virtual environment before installing analysis dependencies.", file=sys.stderr)
        return 1
    filename = "requirements-analysis.txt"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            print("The validated ONNX runtime requires macOS 14 or later.", file=sys.stderr)
            return 1
        filename = "requirements-analysis-macos-arm64-py311.lock.txt"
    requirements = Path(__file__).resolve().with_name(filename)
    print(f"Installing analysis dependencies from {filename}")
    return subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(requirements)])


if __name__ == "__main__":
    raise SystemExit(main())

from importlib.metadata import version, PackageNotFoundError
import tomllib
from pathlib import Path

try:
    __version__ = version("tune-server")
except PackageNotFoundError:
    # Fallback: read from pyproject.toml (works in PyInstaller bundles)
    try:
        _pyproject = Path(__file__).parent.parent / "pyproject.toml"
        if _pyproject.exists():
            with open(_pyproject, "rb") as f:
                __version__ = tomllib.load(f)["project"]["version"]
        else:
            __version__ = "0.2.1"
    except Exception:
        __version__ = "0.2.1"

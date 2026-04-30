from importlib.metadata import PackageNotFoundError, version as _get_version

try:
    __version__ = _get_version("tune-server")
except PackageNotFoundError:
    # PyInstaller bundle without dist-info. Fall back to the env var the
    # release workflow injects, then a sentinel.
    import os as _os
    __version__ = _os.environ.get("TUNE_VERSION", "0.0.0+frozen")

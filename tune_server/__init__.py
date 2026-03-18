from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("tune-server")
except PackageNotFoundError:
    __version__ = "0.1.5"

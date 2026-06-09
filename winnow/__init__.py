"""Winnow media library organizer."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("winnow-media")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

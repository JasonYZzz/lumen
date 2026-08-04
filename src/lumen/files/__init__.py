"""Workspace-confined file mention expansion and completion search."""

from .mentions import expand_file_mentions
from .search import FileHit, FileSearchHandle, build_suggestion, search_files

__all__ = [
    "FileHit",
    "FileSearchHandle",
    "build_suggestion",
    "expand_file_mentions",
    "search_files",
]

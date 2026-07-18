"""Winnow pipeline package: reversible commands and run context.

Exposes the command pattern implementations used for reversible file mutations
and the :class:`PipelineContext` dependency-injection container that wires
services into pipeline steps.
"""

from __future__ import annotations

from winnow.pipeline.commands import (
    Command,
    CopyFile,
    CreateDirectory,
    DeleteFile,
    MoveFile,
)
from winnow.pipeline.context import PipelineContext

__all__ = [
    "Command",
    "CopyFile",
    "CreateDirectory",
    "DeleteFile",
    "MoveFile",
    "PipelineContext",
]

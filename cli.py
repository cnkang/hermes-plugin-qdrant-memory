"""Hermes directory-provider CLI discovery seam."""

from .qdrant_memory.cli import main
from .qdrant_memory.cli import register_cli as register_cli

globals()["qdrant-memory_command"] = main

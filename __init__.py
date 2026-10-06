"""Directory-provider entry point; package providers use qdrant_memory directly."""
if __package__:
    from .qdrant_memory import register as register
else:
    # pytest imports a hyphenated repository root as a standalone module.
    from qdrant_memory import register as register

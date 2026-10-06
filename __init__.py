"""Directory-provider entry point; package providers use qdrant_memory directly."""
def register(ctx):
    if __package__:
        from .qdrant_memory import create_provider
    else:
        from qdrant_memory import create_provider
    # Directory discovery checks this registration call without importing code.
    ctx.register_memory_provider(create_provider(ctx))

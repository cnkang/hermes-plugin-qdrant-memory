"""Native Hermes MemoryProvider registration, with no import-time I/O."""

from .version import VERSION as __version__

__all__ = ["register", "__version__"]


def register(ctx):
    ctx.register_memory_provider(create_provider(ctx))


def create_provider(ctx):
    from .provider import QdrantMemoryProvider

    # Some Hermes memory discovery collectors expose registration only. Build
    # the same named PluginContext so LLM calls still traverse the host trust gate.
    if not hasattr(ctx, "llm"):
        from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager
        runtime_ctx = PluginContext(PluginManifest(name="qdrant-memory", key="qdrant-memory"), get_plugin_manager())
    else:
        runtime_ctx = ctx

    ctx.register_auxiliary_task(
        "qdrant_memory_extraction",
        display_name="Qdrant Memory Extraction",
        description="Extract and reconcile durable memories.",
    )
    return QdrantMemoryProvider(plugin_context=runtime_ctx)

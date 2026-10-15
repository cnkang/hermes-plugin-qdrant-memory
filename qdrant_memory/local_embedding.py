"""Optional, offline-only CPU embeddings over verified model artifacts."""

from importlib.metadata import PackageNotFoundError, version

from .config import fingerprint
from .embedding import validate_vectors
from .local_model import (
    FASTEMBED_VERSION,
    MAX_TOKENS,
    MODEL_NAME,
    validate_local_config,
    verify_model,
)


class LocalEmbeddingUnavailableError(RuntimeError):
    """Require the optional local dependency set without changing providers."""


class LocalEmbeddingInputTooLongError(ValueError):
    """Reject text that the local model would otherwise silently truncate."""


def require_local_dependencies():
    """Fail before downloading when the explicitly requested local extra is absent."""
    try:
        if version("fastembed") != FASTEMBED_VERSION:
            raise LocalEmbeddingUnavailableError("Install the supported local extra")
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer
    except (ImportError, PackageNotFoundError) as exc:
        raise LocalEmbeddingUnavailableError("Install the local extra before use") from exc
    return TextEmbedding, Tokenizer


class LocalEmbeddingProvider:
    """Run the pinned CPU pipeline; startup never installs or downloads anything."""

    def __init__(self, cfg):
        """Verify the prepared cache before loading optional inference dependencies."""
        validate_local_config(cfg)
        model_dir = verify_model(cfg.get("model_cache_dir"))
        TextEmbedding, Tokenizer = require_local_dependencies()
        self.cfg = cfg
        self.dimensions = cfg["dimensions"]
        self.fingerprint = fingerprint(cfg)
        self._tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        self._model = TextEmbedding(
            model_name=MODEL_NAME,
            specific_model_path=str(model_dir),
            local_files_only=True,
            providers=["CPUExecutionProvider"],
            cuda=False,
            threads=2,
        )

    def _embed(self, texts):
        if self._model is None:
            raise LocalEmbeddingUnavailableError("Embedding provider is closed")
        # Check the entire batch before inference, including instruction prefixes.
        for text in texts:
            if len(self._tokenizer.encode(text).ids) > MAX_TOKENS:
                raise LocalEmbeddingInputTooLongError("Local embedding input exceeds 128 tokens")
        vectors = [
            vector.tolist()
            for vector in self._model.embed(
                texts,
                batch_size=self.cfg["batch_size"],
                parallel=None,
            )
        ]
        return validate_vectors(vectors, len(texts), self.dimensions)

    def embed_documents(self, texts):
        """Embed documents without silent token truncation or network access."""
        prefix = self.cfg.get("document_instruction", "")
        return self._embed([prefix + text for text in texts])

    def embed_query(self, text):
        """Embed one query under the same pinned pipeline identity."""
        return self._embed([self.cfg.get("query_instruction", "") + text])[0]

    def close(self):
        """Release references to the CPU session; no cache or data deletion."""
        self._model = None

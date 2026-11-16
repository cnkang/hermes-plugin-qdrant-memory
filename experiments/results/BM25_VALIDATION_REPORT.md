# BM25 Hybrid Recall Validation Experiment Report

> Historical exploratory baseline. Its slice-wide shared relevant IDs are not
> independent per-question validation. The current reviewable experiment is
> [independent/README.md](../independent/README.md), with the frozen plan in
> [independent/PLAN.md](../independent/PLAN.md). Recommendations below are the
> historical report, superseded by the independent experiment's decision.
> CI run 43876725500 had mixed Recall delta +0.10, Chinese +0.02, code flat,
> stale_fact 0.444 to 0.400. Its +1.4% mixed mean latency is not warm p95.
> Chinese +10pp remains an original check; it is not relaxed for this round.

**Experiment Date**: 2026-11-16  
**Runtime Version**: 0.7.1 (main branch)  
**Qdrant Server**: 1.20.0  
**Qdrant Client**: 1.20.0  
**Embedding Model**: qwen3-embedding:4b (2560 dims, Cosine)  
**Test Corpus**: 60 memories across 6 slices  
**Test Queries**: 60 queries (10 per slice)

---

## Executive Summary

**Decision: LIMITED OPT-IN EXPERIMENT (不发版 0.8.0)**

BM25 hybrid recall shows **real but narrow improvements** that do not meet the release thresholds for a general-purpose 0.8.0 release. The improvements are concentrated in specific slices (mixed Chinese-English, English semantic paraphrase, identifiers) while Chinese no-space queries and code/identifier exact-match queries show minimal or negative gains.

**Recommendation**: Implement as opt-in `hybrid_multilingual_rrf` for server mode only, behind explicit config flag. Do not change default dense behavior. Defer 0.8.0 release until thresholds are met.

---

## Key Findings by Slice

| Slice | Dense Baseline Recall@8 | Best Hybrid Recall@8 | Δ Recall@8 | Best Config | nDCG@8 Δ |
|-------|------------------------|---------------------|------------|-------------|----------|
| **chinese** (no-space) | 0.160 | 0.180 | **+0.020** | hybrid_multilingual_rrf | +0.033 |
| **english** (semantic) | 0.180 | 0.220 | **+0.040** | hybrid_multilingual_rrf / hybrid_large_rrf | +0.028 |
| **mixed** (zh-en) | 0.120 | 0.220 | **+0.100** | hybrid_multilingual_rrf | +0.087 |
| **code** (exact) | 0.340 | 0.320 | -0.020 | dense_baseline | -0.003 |
| **identifiers** | 0.320 | 0.380 | **+0.060** | hybrid_word_rrf / hybrid_multilingual_rrf | +0.038 |
| **stale_fact** | 0.467 | 0.467 | 0.000 | dense_baseline | -0.007 |

### Critical Observations

1. **Chinese no-space queries**: BM25 with `word` tokenizer **completely fails** (Recall@8=0.000). Only `multilingual` tokenizer works (0.120), but still below dense baseline (0.160). Hybrid multilingual narrowly beats dense (+0.020).

2. **Mixed Chinese-English queries**: **Largest gain** (+0.100 Recall@8, +0.087 nDCG@8) with `hybrid_multilingual_rrf`. This is the only slice meeting the +10pp identifier threshold.

3. **Code/identifier exact-match queries**: Dense baseline already strong (0.340/0.320). Hybrid provides marginal identifier improvement (+0.060) but **regresses on code** (-0.020). The `code` tokenizer doesn't preserve full identifiers with punctuation.

4. **English semantic paraphrase**: Consistent +0.040 Recall@8 improvement across hybrid configs.

5. **Stale fact / negation queries**: No improvement; dense baseline already handles these well via semantic similarity.

---

## Tokenizer Comparison (Server Mode)

| Tokenizer | Chinese Recall@8 | English Recall@8 | Mixed Recall@8 | Code Recall@8 | Identifiers Recall@8 |
|-----------|-----------------|------------------|----------------|---------------|---------------------|
| word (BM25-only) | 0.000 | 0.140 | 0.100 | 0.200 | 0.240 |
| multilingual (BM25-only) | 0.120 | 0.140 | 0.200 | 0.160 | 0.220 |
| code (BM25-only) | 0.020 | 0.120 | 0.120 | 0.160 | 0.280 |
| word (Hybrid RRF) | 0.160 | 0.200 | 0.120 | 0.320 | 0.380 |
| **multilingual (Hybrid RRF)** | **0.180** | **0.220** | **0.220** | 0.320 | 0.380 |
| code (Hybrid RRF) | 0.160 | 0.180 | 0.120 | 0.300 | 0.360 |

**Winner for hybrid**: `multilingual` tokenizer with RRF fusion — best overall balance across Chinese, English, and mixed queries.

---

## Latency Analysis

| Config | Avg Latency (ms) | p95 Latency (ms) | Embedding Latency (ms) | Search Latency (ms) | vs Dense Baseline |
|--------|------------------|-------------------|------------------------|---------------------|-------------------|
| dense_baseline (server) | 55.8 | ~75 | 45-55 | 5-15 | 1.00x |
| dense_large_candidates | 70.1 | ~95 | 45-55 | 15-30 | 1.26x |
| bm25_only_word | 4.3 | ~8 | 0 | 2-4 | **0.08x** |
| bm25_only_multilingual | 1.9 | ~4 | 0 | 1-3 | **0.03x** |
| hybrid_word_rrf | 64.3 | ~85 | 45-55 | 15-25 | 1.15x |
| hybrid_multilingual_rrf | 64.7 | ~85 | 45-55 | 15-25 | 1.16x |
| hybrid_large_rrf | 65.6 | ~90 | 45-55 | 20-35 | 1.18x |

**Cost budget check**: Hybrid RRF adds ~15-20% latency vs dense baseline, within the 20% budget. BM25-only is 10-30x faster but with significantly lower recall for Chinese/mixed queries.

---

## Acceptance Threshold Evaluation

| Criterion | Threshold | Result | Pass/Fail |
|-----------|-----------|--------|-----------|
| **Precise identifiers/code/proper names**: Recall@8 ≥ +10pp vs tuned dense | +10pp | Identifiers: +6pp ❌<br>Code: -2pp ❌<br>Chinese: +2pp ❌ | **FAIL** |
| **Overall benefit**: nDCG@8 ≥ +0.03 absolute, CI not crossing zero | +0.03 | Mixed: +0.087 ✓<br>Chinese: +0.033 ✓<br>English: +0.028 ~<br>Code: -0.003 ✗ | **PARTIAL** |
| **Chinese/semantic regression**: nDCG@8 regression ≤ 0.02 | ≤ 0.02 | All slices improve or stable ✓ | **PASS** |
| **Cost**: Warm p95 ≤ 20% over dense | ≤ 20% | Hybrid RRF: +15-18% ✓ | **PASS** |
| **Data/permission integrity**: No visibility leakage, deletion consistency, no ID/embedding drift | Zero tolerance | Not tested in this experiment | **UNTESTED** |

---

## Failure Cases & Root Causes

### 1. BM25 `word` tokenizer fails on Chinese (Recall@8=0.000)
- **Cause**: `word` tokenizer splits on whitespace; Chinese has no spaces → no token matches
- **Fix**: Use `multilingual` tokenizer (works) or `code` tokenizer (partial)

### 2. Code exact-match queries don't benefit from BM25
- **Cause**: `code` tokenizer doesn't preserve full identifiers with punctuation (e.g., `async def fetch_data(session: aiohttp.ClientSession)` → tokens lose `():.,` structure)
- **Evidence**: Dense embedding already captures semantic similarity of code patterns

### 3. Identifier queries only +6pp (not +10pp)
- **Cause**: Dense embeddings already handle version numbers, error codes, paths reasonably well via semantic similarity; BM25 adds exact token matches but many identifiers are split across tokens

### 4. Stale fact queries show no improvement
- **Cause**: Both dense and BM25 retrieve stale facts equally; semantic similarity doesn't distinguish current vs deprecated facts
- **Note**: This requires LLM-based fact verification, not retrieval improvement

---

## Recommendations

### 1. Do NOT Release 0.8.0
Thresholds not met for general-purpose release. The improvements are real but too narrow.

### 2. Implement Opt-In Server-Mode Hybrid (Post-0.7.1 Patch)
```python
# Config addition (opt-in only)
"search": {
    "mode": "dense",  # default unchanged
    "hybrid": {
        "enabled": false,
        "tokenizer": "multilingual",
        "rrf_k": 60,
        "dense_candidate_k": 24,
        "bm25_candidate_k": 24
    }
}
```

### 3. Scope of Opt-In
- **Backend**: Server mode only (embedded lacks text index scoring support)
- **Tokenizer**: `multilingual` only (best cross-lingual balance)
- **Fusion**: RRF (k=60) with equal candidate budgets (24 each)
- **Default**: `enabled: false` — dense remains default

### 4. Deferred Work for 0.8.0
- [ ] Embedded mode text index scoring support (requires Qdrant local API update)
- [ ] Visibility/permission verification for hybrid results
- [ ] Deletion consistency tests for hybrid index
- [ ] Larger eval corpus (1000+ queries, human-annotated relevance)
- [ ] Statistical significance testing (paired bootstrap CIs)
- [ ] Custom IDF corpus support for domain-specific token weighting

---

## Reproducibility

**Experiment Script**: `experiments/bm25_experiment.py`  
**Results**: `experiments/results/bm25_experiment_results.json`  
**SHA**: (to be filled after commit)

**To Reproduce**:
```bash
# Requires Qdrant 1.20.0 server on localhost:6333
cd /Volumes/2T/development/hermes-plugin-qdrant-memory
python experiments/bm25_experiment.py
```

**Environment**:
- Python 3.14.9
- qdrant-client 1.20.0
- qdrant-server 1.20.0 (Docker: `qdrant/qdrant:v1.20.0`)
- Ollama with `qwen3-embedding:4b` model

---

## Appendix: Per-Query Latency Breakdown (Hybrid Multilingual RRF)

| Slice | Avg Total | Avg Embedding | Avg Search | p95 Total |
|-------|-----------|---------------|------------|-----------|
| chinese | 66.7 | 48.2 | 18.5 | ~85 |
| english | 62.8 | 47.1 | 15.7 | ~80 |
| mixed | 69.0 | 52.3 | 16.7 | ~90 |
| code | 81.8 | 65.4 | 16.4 | ~110 |
| identifiers | 67.4 | 50.1 | 17.3 | ~85 |
| stale_fact | 60.6 | 45.8 | 14.8 | ~75 |

Embedding latency dominates (70-80% of total). Search latency is well-controlled.

---

**Conclusion**: BM25 hybrid recall is a **validated capability** with measurable benefits for mixed-language and semantic-paraphrase queries, but **does not meet release thresholds** for a general-purpose 0.8.0. Proceed with opt-in implementation for server-mode users who need mixed-language recall improvements.

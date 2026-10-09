"""Ranking metrics with complete positive-judgment denominators."""
import math


def metrics(ranked, relevance, k=10):
    if type(k) is not int or k < 1:
        raise ValueError("k must be a positive integer")
    if len(set(ranked)) != len(ranked):
        raise ValueError("duplicate source keys in ranking")
    if not relevance or any(type(v) is not int or not 0 < v <= 4 for v in relevance.values()):
        raise ValueError("positive integer judgments in 1..4 are required")
    chosen = ranked[:k]
    dcg = sum((2 ** relevance.get(doc, 0) - 1) / math.log2(rank + 2)
              for rank, doc in enumerate(chosen))
    ideal = sum((2 ** grade - 1) / math.log2(rank + 2)
                for rank, grade in enumerate(sorted(relevance.values(), reverse=True)[:k]))
    return {f"recall_at_{k}": sum(doc in relevance for doc in chosen) / len(relevance),
            f"ndcg_at_{k}": dcg / ideal,
            f"mrr_at_{k}": next((1 / (rank + 1) for rank, doc in enumerate(chosen)
                                 if doc in relevance), 0.0)}


def stable(value):
    import hashlib
    return hashlib.sha256(("pg-qdrant-quality-v1\0" + value).encode("utf-8")).digest()


def select_documents(documents, relevance, max_points=500, max_bytes=1024 * 1024):
    """Retain every positive and only hash-selected decoys; never truncate text."""
    required = {doc for judgments in relevance.values() for doc in judgments}
    if not required or not required <= documents.keys() or len(required) > max_points:
        raise ValueError("missing or over-budget positive documents")
    if any(len(documents[doc].encode("utf-8")) > 65536 for doc in documents):
        raise ValueError("source body exceeds 64 KiB")
    kept = sorted(required, key=stable)
    size = sum(len(documents[doc].encode("utf-8")) for doc in kept)
    if size > max_bytes:
        raise ValueError("positive documents exceed source byte budget")
    decoys = sorted(documents.keys() - required, key=stable)[:max_points - len(kept)]
    for doc in decoys:
        length = len(documents[doc].encode("utf-8"))
        if size + length > max_bytes:
            break
        kept.append(doc)
        size += length
    return kept, size

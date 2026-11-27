"""Request-local Okapi BM25 for one video's bounded chunk corpus."""

from collections import Counter
from collections.abc import Sequence
import math
import re
import unicodedata

from .rank_fusion import RankedCandidate

_TOKENS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9_]+(?:(?:::|[.+/#:-])[a-z0-9_]+)*(?:\+\+|#)?|[+#]")



def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def tokenize(text: str) -> tuple[str, ...]:
    """Chinese unigrams + bigrams; Latin/numeric/identifier compounds.

    Keep exact technical compounds (flashattention-2, b+tree, qkv, foo_bar)
    and their components. Query repeats are collapsed by the BM25 scorer.
    """
    result = []
    for match in _TOKENS.finditer(normalize_text(text)):
        token = match.group()
        if "\u3400" <= token[0] <= "\u9fff":
            result.extend(token)
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
            parts = re.split(r"[.+/#:-]", token)
            if len(parts) > 1:
                result.extend(part for part in parts if part)
    return tuple(result)

BM25_K1 = 1.2
BM25_B = 0.75


class BM25Retriever:
    def rank(self, query: str, documents: Sequence[str], *, limit: int) -> tuple[RankedCandidate, ...]:
        if not query.strip() or not documents or limit <= 0:
            return ()
        terms = tuple(dict.fromkeys(tokenize(query)))
        frequencies = [Counter(tokenize(doc)) for doc in documents]
        lengths = [sum(tf.values()) for tf in frequencies]
        average = sum(lengths) / len(lengths)
        if not terms or not average:
            return ()
        document_frequency = Counter(term for tf in frequencies for term in tf)
        result = []
        for index, (tf, length) in enumerate(zip(frequencies, lengths, strict=True)):
            score = 0.0
            normalization = BM25_K1 * (1 - BM25_B + BM25_B * length / average)
            for term in terms:
                frequency = tf[term]
                if frequency:
                    df = document_frequency[term]
                    idf = math.log(1 + (len(documents) - df + 0.5) / (df + 0.5))
                    score += idf * frequency * (BM25_K1 + 1) / (frequency + normalization)
            if score > 0:
                result.append(RankedCandidate(index, score))
        return tuple(sorted(result, key=lambda item: (-item.score, item.index))[:limit])

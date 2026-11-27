import math

import pytest

from cutsieve.application.sparse_retrieval import BM25Retriever
from cutsieve.application.rank_fusion import RankedCandidate, reciprocal_rank_fusion


def rank(query, docs):
    return BM25Retriever().rank(query, docs, limit=8)


def test_bm25_formula_tf_idf_and_length_normalization():
    docs = ["rare rare common", "common filler filler filler filler", "common"]
    result = rank("rare", docs)
    avg = (3 + 5 + 1) / 3
    expected = math.log(1 + (3 - 1 + .5) / (1 + .5)) * 2 * 2.2 / (2 + 1.2 * (.25 + .75 * 3 / avg))
    assert result[0].score == pytest.approx(expected)
    assert rank("rare", ["rare rare", "rare filler"])[0].index == 0
    assert rank("rare", ["rare filler filler", "rare"])[0].index == 1
    assert rank("rare", ["rare", "common", "common"])[0].score > rank("common", ["rare", "common", "common"])[0].score


@pytest.mark.parametrize("query,document", [("中文检索", "解释中文检索系统"), ("QKV", "QKV matrix"),
    ("2026", "version 2026"), ("FlashAttention-2", "FlashAttention-2 memory"),
    ("B+Tree", "B+Tree index"), ("foo_bar", "call foo_bar"), ("ＦＯＯ", "foo"), ("std::vector", "std::vector"),
    ("C++", "C++ language"), ("C#", "C# language")])
def test_multilingual_numbers_technical_identifiers(query, document):
    assert rank(query, ["unrelated", document])[0].index == 1


def test_empty_repeat_and_stable_ties():
    assert rank("", ["foo"]) == ()
    assert rank("foo", []) == ()
    assert rank("foo", ["", " "]) == ()
    assert rank("foo foo", ["foo", "foo"]) == rank("foo", ["foo", "foo"])
    assert [c.index for c in rank("foo", ["foo", "foo"])] == [0, 1]
    assert rank("foo", ["foobar"]) == ()


def candidates(*indexes):
    return tuple(RankedCandidate(i, 999_999 - i) for i in indexes)


@pytest.mark.parametrize("arms,order", [((candidates(2, 1), ()), [2, 1]),
    (((), candidates(1, 2)), [1, 2]), ((candidates(0, 1), candidates(1, 2)), [1, 0, 2]),
    ((candidates(1, 0), candidates(0, 1)), [0, 1]), (((), ()), [])])
def test_rrf_single_overlap_disagreement_ties_empty(arms, order):
    assert [c.index for c in reciprocal_rank_fusion(arms, limit=10)] == order


def test_rrf_is_rank_only_caps_and_distinct_per_arm():
    out = reciprocal_rank_fusion((candidates(2, 2, 1),), limit=1)
    assert out == (RankedCandidate(2, 1 / 61),)
    assert reciprocal_rank_fusion((candidates(0, 1),), limit=2)[1].score == 1 / 62
    with pytest.raises(ValueError):
        reciprocal_rank_fusion((), limit=2, k=0)

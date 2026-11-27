import pytest

from cutsieve.application.media_memory import ocr_occurrences, speech_flags, viewing_notes
from cutsieve.infrastructure.media.visual_observations import validated_claims


def test_ocr_unbroken_text_merges_but_reappearing_text_is_a_new_occurrence():
    label = dict(text="请把钥匙放在桌上", polygon=[[1, 2], [30, 2], [30, 12], [1, 12]], confidence=0.98)
    frames = [(0, [label]), (500000, [label]), (1000000, []), (1200000, [label]), (5000000, [label])]
    groups = ocr_occurrences(frames)
    assert [(g["start_us"], g["end_us"]) for g in groups] == [
        (0, 500000),
        (1200000, 1200000),
        (5000000, 5000000),
    ]


def test_notes_depend_only_on_cited_children():
    row = lambda rid, start, text: dict(
        evidence_id=rid,
        range=dict(start_us=start, end_us=start + 1),
        claim=text,
        support="SUPPORTED",
        source="embedded-subtitle",
    )
    values = [row("a", 100, "门开了"), row("b", 11_000_000, "关灯")]
    spans = [(0, 10_000_000), (8_000_000, 20_000_000)]
    before = viewing_notes(20_000_000, values, spans)
    assert before["segments"][0]["evidence_ids"] == ["a"]
    assert before["chapters"][0]["claims"][1]["evidence_id"] == "b"
    values[1]["claim"] = "点灯"
    changed = viewing_notes(20_000_000, values, spans)
    assert before["segments"][0] == changed["segments"][0]
    assert before["segments"][1]["dependency_sha256"] != changed["segments"][1]["dependency_sha256"]


def test_visual_claims_reject_forged_frame_or_single_frame_sequence():
    allowed = {"one": 4, "two": 8}
    claim = dict(text="两人面对面站着", frames=["one"], claim_type="OBSERVED", temporal=False)
    assert validated_claims(dict(observations=[claim]), allowed) == [claim]
    for change in (
        dict(frames=["invented"]),
        dict(temporal=True),
        dict(temporal=True, frames=["two", "one"]),
        dict(workspace_id="forged"),
    ):
        with pytest.raises(ValueError):
            validated_claims(dict(observations=[dict(claim, **change)]), allowed)


def test_repeat_verification_preserves_distinct_utterances_and_segment_only_times():
    history = {}
    words = dict(text="Open the door", words=[], no_speech_prob=.1, avg_logprob=-.2)
    assert speech_flags(words, 1_000_000, 2_000_000, history) == ["SEGMENT_LEVEL_TIMING"]
    assert speech_flags(words, 1_050_000, 2_060_000, history) is None
    second = dict(words, compression_ratio=3)
    assert speech_flags(second, 32_000_000, 33_000_000, history) == [
        "SEGMENT_LEVEL_TIMING", "REPEATED_TEXT", "LOW_SPEECH_CONFIDENCE"
    ]

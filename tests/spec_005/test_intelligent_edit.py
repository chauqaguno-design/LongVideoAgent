import pytest
from dovideo.application.edit_instructions import choose_intervals, literal_plan
from dovideo.application.workflow_routing import replacement_request
from dovideo.application.workflow_state import NeedsInput


def test_chinese_audio_and_clip_instructions():
    assert literal_plan("剪出台词「Wait a minute」并保留原声") == dict(
        command="quote", query="Wait a minute", first=False, audio="KEEP_SOURCE"
    )
    assert literal_plan("剪出台词「不要静音」并静音")["audio"] == "MUTE"
    assert literal_plan("保留原声，将第 1 至 3 秒和第 7 至 9 秒依次拼接")["ranges"] == [
        dict(start_us=1_000_000, end_us=3_000_000),
        dict(start_us=7_000_000, end_us=9_000_000),
    ]
    with pytest.raises(NeedsInput) as conflict:
        literal_plan("剪出台词「Wait」并保留原声和静音")
    assert conflict.value.input["kind"] == "CONSTRAINT_CONFLICT"


def test_stable_last_clip_replacement_and_duration_selection():
    message = "把最后一段换成剪出 1 到 3 秒"
    assert replacement_request(message) == (-1, "剪出 1 到 3 秒")
    assert literal_plan(message)["ranges"] == [dict(start_us=1_000_000, end_us=3_000_000)]
    candidates = [
        ("media", dict(start_us=i, end_us=i + n), ["evidence"]) for i, n in [(0, 1), (3, 3), (8, 2)]
    ]
    assert choose_intervals(dict(duration_target_us=5, duration_tolerance_us=0), candidates) == candidates[1:]
    with pytest.raises(NeedsInput):
        choose_intervals(dict(duration_target_us=10, duration_tolerance_us=0), candidates)

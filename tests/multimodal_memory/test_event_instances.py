from cutsieve.application.media_memory import viewing_notes


def test_repeated_gesture_survives_as_two_instances_with_overlapping_segments():
    def gesture(eid, start, end):
        return dict(
            evidence_id=eid,
            source="visual-observer",
            claim="女子向窗边挥手",
            support="SUPPORTED",
            range=dict(start_us=start, end_us=end),
        )

    rows = [
        gesture("a", 8_500_000, 9_800_000),
        gesture("b", 9_000_000, 10_500_000),
        gesture("c", 21_000_000, 22_000_000),
    ]
    ranges = [(0, 10_000_000), (8_000_000, 18_000_000), (16_000_000, 26_000_000)]
    notes = viewing_notes(26_000_000, rows, ranges)
    assert [event["evidence_ids"] for event in notes["events"]] == [["a", "b"], ["c"]]
    assert notes["segments"][0]["event_ids"] == notes["segments"][1]["event_ids"]
    assert notes["segments"][2]["event_ids"] != notes["segments"][0]["event_ids"]
    assert notes["overview"]["highlights"][0]["evidence_id"] == "a"
    rows[-1]["claim"] = "女子把手放下"
    changed = viewing_notes(26_000_000, rows, ranges)
    assert notes["segments"][0]["dependency_sha256"] == changed["segments"][0]["dependency_sha256"]
    assert notes["overview"]["dependency_sha256"] != changed["overview"]["dependency_sha256"]

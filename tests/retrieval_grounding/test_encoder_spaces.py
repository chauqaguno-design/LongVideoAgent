from cutsieve.infrastructure.search_encoder import SearchEncoder


def test_same_dimensions_do_not_authorize_another_checkpoint(tmp_path, monkeypatch):
    encoder = SearchEncoder("http://127.0.0.1:18174", tmp_path)
    model = {"dimensions": 3, "version": "checkpoint-A"}
    count = []

    def exchange(path, body=None):
        if path == "/spaces":
            return {"text": dict(model)}
        count.append(len(body["items"]))
        return {
            "version": model["version"],
            "vectors": [[1, 0, 0] for item in body["items"]],
        }

    monkeypatch.setattr(encoder, "call", exchange)
    row = {
        "workspace_id": "workspace",
        "source_generation_id": "immutable-run",
        "source": "embedded-subtitle",
        "claim": "the red car",
    }
    encoder.prepare("workspace", [row])
    encoder.prepare("workspace", [row])
    assert count == [1]
    routes = {}
    encoder.rank("red car", None, [row], routes, [])
    assert routes["text_dense"][1][0].score == 1
    model["version"] = "checkpoint-B"
    routes, warnings = {}, []
    encoder.rank("red car", None, [row], routes, warnings)
    assert routes == {} and warnings

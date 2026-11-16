import pytest

from jsonref import JsonRefError, JsonLoader, LoadState, replace_refs


class MemoryLoader:
    def __init__(self, docs):
        self.docs = docs
        self.calls = []

    def __call__(self, uri):
        self.calls.append(uri)
        if uri not in self.docs:
            raise FileNotFoundError(uri)
        return self.docs[uri]


def build(loader):
    return replace_refs(
        {
            "first": {"$ref": "late.json#/first"},
            "second": {"$ref": "late.json#/second"},
            "normal": {"$ref": "normal.json#/value"},
        },
        base_uri="root.json",
        loader=loader,
        load_on_repr=False,
    )


def test_shared_document_starts_unvisited_and_reports_paths():
    loader = MemoryLoader(
        {"normal.json": {"value": "ok"}, "late.json": {"first": 1, "second": 2}}
    )
    result = build(loader)

    assert result["first"].state is LoadState.UNVISITED
    assert result["second"].state is LoadState.UNVISITED
    assert result["second"].state == "unvisited"
    assert result["first"].affected_paths == (("first",), ("second",))
    assert result["first"].document_uri == "late.json"


def test_failure_is_shared_but_other_document_is_unaffected():
    loader = MemoryLoader({"normal.json": {"value": "ok"}})
    result = build(loader)

    with pytest.raises(JsonRefError) as excinfo:
        result["first"].__subject__

    assert result["first"].state is LoadState.FAILED
    assert result["second"].state is LoadState.FAILED
    assert result["first"].load_error is excinfo.value
    assert result["second"].load_error is excinfo.value
    assert result["normal"].__subject__ == "ok"
    assert result["normal"].state is LoadState.LOADED
    assert result["first"].affected_paths == (("first",), ("second",))


def test_failure_is_retried_when_the_document_arrives():
    loader = MemoryLoader({"normal.json": {"value": "ok"}})
    result = build(loader)

    with pytest.raises(JsonRefError):
        result["first"].__subject__
    assert loader.calls == ["late.json"]

    # A plain retry before the file exists fails again, without caching.
    with pytest.raises(JsonRefError):
        result["second"].__subject__
    assert loader.calls == ["late.json", "late.json"]

    loader.docs["late.json"] = {"first": 1, "second": 2}
    assert result["second"].__subject__ == 2
    assert result["second"].state is LoadState.LOADED
    assert result["first"].__subject__ == 1
    assert result["first"].state is LoadState.LOADED
    assert loader.calls == ["late.json", "late.json", "late.json"]

    # The successful document is cached; later accesses do not call again.
    assert result["first"].__subject__ == 1
    assert loader.calls == ["late.json", "late.json", "late.json"]


def test_explicit_retry_after_supplying_document():
    loader = MemoryLoader({"normal.json": {"value": "ok"}})
    result = build(loader)

    with pytest.raises(JsonRefError):
        result["first"].__subject__

    loader.docs["late.json"] = {"first": 3, "second": 4}
    assert result["first"].retry() == 3
    assert result["first"].state is LoadState.LOADED
    assert result["normal"].__subject__ == "ok"
    assert result["normal"].state is LoadState.LOADED


def test_failure_leaves_no_half_parsed_document_in_cache():
    class BrokenLoader:
        def __init__(self):
            self.calls = 0

        def __call__(self, uri):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("still being written")
            return {"first": 5}

    loader = BrokenLoader()
    result = replace_refs(
        {"first": {"$ref": "late.json#/first"}},
        base_uri="root.json",
        loader=loader,
        load_on_repr=False,
    )
    ref = result["first"]

    with pytest.raises(JsonRefError):
        ref.__subject__
    assert ref.state is LoadState.FAILED
    # The document that failed to load was never published to the cache.
    assert "late.json" not in object.__getattribute__(ref, "store")

    # The retry starts from a clean cache and can succeed.
    assert ref.retry() == 5
    assert ref.state is LoadState.LOADED


def test_json_loader_tracks_state_and_paths():
    loader = JsonLoader(loader=lambda uri: {"first": 7})
    result = replace_refs(
        {"first": {"$ref": "late.json#/first"}, "second": {"$ref": "late.json#/first"}},
        base_uri="root.json",
        loader=loader,
        load_on_repr=False,
    )

    assert loader.state("late.json") is LoadState.UNVISITED
    assert result["first"].state is LoadState.UNVISITED
    assert result["second"].affected_paths == (("first",), ("second",))

    assert result["first"].__subject__ == 7
    assert loader.state("late.json") is LoadState.LOADED
    assert result["second"].state is LoadState.LOADED
    assert loader.affected_paths("late.json") == (("first",), ("second",))

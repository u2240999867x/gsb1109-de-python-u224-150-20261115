import warnings

import pytest

from jsonref.cache import (
    CacheRecord,
    JsonLoader,
    ParseCache,
    StaleSnapshotWarning,
    canonical_uri,
    digest_dependencies,
    document_digest,
)


DOCUMENTS = {
    "main.json": {"root": {"$ref": "dictionary.json#/term"}},
    "other.json": {"root": {"$ref": "shared.json#/value"}},
    "dictionary.json": {"term": {"name": "old"}},
    "shared.json": {"value": {"name": "shared"}},
}


def loader_for(documents, calls=None):
    def load(uri):
        if calls is not None:
            calls.append(uri)
        if isinstance(documents[uri], Exception):
            raise documents[uri]
        return documents[uri]

    return load


class TestDependencyDigest(object):
    def test_root_digest_covers_external_targets(self):
        cache = JsonLoader(DOCUMENTS)
        before = cache.snapshot("main.json")
        assert before.dependencies == {
            "dictionary.json": document_digest(DOCUMENTS["dictionary.json"])
        }

        DOCUMENTS["dictionary.json"] = {"term": {"name": "new"}}
        try:
            after = cache.refresh("main.json")
        finally:
            DOCUMENTS["dictionary.json"] = {"term": {"name": "old"}}

        assert after.document == {"root": {"$ref": "dictionary.json#/term"}}
        assert after.dependencies != before.dependencies
        assert after.digest == before.digest

    def test_dependency_summary_includes_transitive_documents(self):
        documents = {
            "a.json": {"b": {"$ref": "b.json#/b"}},
            "b.json": {"b": {"$ref": "c.json#/c"}},
            "c.json": {"c": 1},
        }
        cache = JsonLoader(documents)
        record = cache.snapshot("a.json")
        assert set(record.dependencies) == {"b.json", "c.json"}
        assert record.dependencies["c.json"] == document_digest(documents["c.json"])

    def test_dependency_digest_is_order_independent(self):
        documents = {
            "a.json": {
                "one": {"$ref": "b.json#/b"},
                "two": {"$ref": "c.json#/c"},
            },
            "b.json": {"b": 1},
            "c.json": {"c": 2},
        }
        cache = JsonLoader(documents)
        record = cache.snapshot("a.json")
        reordered = dict(reversed(list(record.dependencies.items())))
        assert document_digest(record.dependencies) == document_digest(reordered)


class TestInvalidation(object):
    def test_target_update_invalidates_affected_result(self):
        documents = dict(DOCUMENTS)
        cache = JsonLoader(documents)
        cache.snapshot("main.json")
        cache.snapshot("other.json")

        documents["dictionary.json"] = {"term": {"name": "new"}}
        invalidated = cache.invalidate("dictionary.json")

        assert "main.json" in invalidated
        assert cache.cache.lookup("main.json") is None
        assert cache.cache.lookup("other.json") is not None

    def test_shared_unchanged_target_is_reused(self):
        documents = {
            "main.json": {"a": {"$ref": "shared.json#/x"}},
            "other.json": {"b": {"$ref": "shared.json#/x"}},
            "shared.json": {"x": 1},
        }
        cache = JsonLoader(documents)
        main = cache.snapshot("main.json")
        other = cache.snapshot("other.json")
        shared = cache.snapshot("shared.json")

        documents["shared.json"] = {"x": 2}
        cache.invalidate("shared.json")
        rebuilt = cache.snapshot("main.json")
        reused = cache.snapshot("other.json")

        assert rebuilt.document == main.document
        assert rebuilt.dependencies["shared.json"] != main.dependencies["shared.json"]
        assert reused.document == other.document
        assert reused.dependencies["shared.json"] != other.dependencies["shared.json"]
        assert cache.cache.lookup("shared.json").digest != shared.digest

    def test_unrelated_results_survive_target_update(self):
        documents = {
            "one.json": {"x": {"$ref": "target.json#/v"}},
            "two.json": {"y": {"$ref": "sibling.json#/v"}},
            "target.json": {"v": 1},
            "sibling.json": {"v": 2},
        }
        cache = JsonLoader(documents)
        one = cache.snapshot("one.json")
        two = cache.snapshot("two.json")

        documents["target.json"] = {"v": 99}
        cache.invalidate("target.json")

        assert cache.snapshot("two.json") is two
        assert cache.snapshot("one.json").dependencies["target.json"] != (
            one.dependencies["target.json"]
        )

    def test_explicit_update_returns_invalidated_uris(self):
        documents = {
            "main.json": {"a": {"$ref": "target.json#/v"}},
            "target.json": {"v": 1},
        }
        cache = JsonLoader(documents)
        cache.snapshot("main.json")
        invalidated = cache.cache.update("target.json", {"v": 2})
        assert invalidated == {"main.json"}
        assert cache.cache.lookup("target.json").digest == document_digest({"v": 2})


class TestFailedRefresh(object):
    def test_failed_refresh_keeps_old_snapshot_marked_stale(self):
        documents = dict(DOCUMENTS)
        cache = JsonLoader(loader_for(documents))
        original = cache.snapshot("main.json")

        documents["dictionary.json"] = RuntimeError("dictionary unavailable")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            refreshed = cache.refresh("main.json")

        assert refreshed is original
        assert refreshed.stale is True
        assert refreshed.status == "stale"
        assert refreshed.error is not None
        assert "dictionary unavailable" in str(refreshed.error)
        assert any(issubclass(w.category, StaleSnapshotWarning) for w in caught)
        documents["dictionary.json"] = DOCUMENTS["dictionary.json"]

    def test_failed_refresh_does_not_mix_new_and_old_targets(self):
        documents = {
            "main.json": {
                "changed": {"$ref": "new.json#/value"},
                "unchanged": {"$ref": "old.json#/value"},
            },
            "new.json": {"value": "first"},
            "old.json": {"value": "stable"},
        }
        cache = JsonLoader(loader_for(documents))
        original = cache.snapshot("main.json")

        documents["main.json"] = {
            "changed": {"$ref": "new.json#/value"},
            "unchanged": {"$ref": "old.json#/value"},
            "extra": "not installed",
        }
        documents["new.json"] = {"value": "second"}
        documents["old.json"] = RuntimeError("old target failed")

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            refreshed = cache.refresh("main.json")

        assert refreshed is original
        assert refreshed.stale is True
        assert "extra" not in refreshed.document
        assert refreshed.document["changed"] == documents["main.json"]["changed"]
        assert cache.cache.lookup("new.json").digest == original.dependencies[
            "new.json"
        ]

    def test_failed_refresh_without_previous_snapshot_raises(self):
        documents = {"entry.json": RuntimeError("missing")}
        cache = JsonLoader(loader_for(documents))
        with pytest.raises(RuntimeError):
            cache.snapshot("entry.json")

    def test_stale_snapshot_is_not_reported_as_fresh(self):
        documents = {"entry.json": {"a": 1}}
        cache = JsonLoader(loader_for(documents))
        record = cache.snapshot("entry.json")
        cache.mark_stale("entry.json", error=RuntimeError("expired"))
        documents["entry.json"] = RuntimeError("still unavailable")

        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            stale = cache.snapshot("entry.json")

        assert stale.stale is True
        assert stale.status == "stale"
        assert stale is record

    def test_allow_stale_false_raises_on_failure(self):
        documents = {"entry.json": {"a": 1}}
        cache = JsonLoader(loader_for(documents))
        cache.snapshot("entry.json")
        documents["entry.json"] = RuntimeError("gone")
        with pytest.raises(RuntimeError):
            cache.refresh("entry.json", allow_stale=False)

    def test_refresh_of_stale_source_rebuilds_instead_of_reusing(self):
        documents = {"entry.json": {"a": 1}}
        cache = JsonLoader(loader_for(documents))
        cache.snapshot("entry.json")
        cache.mark_stale("entry.json", error=RuntimeError("expired"))

        documents["entry.json"] = {"a": 2}
        refreshed = cache.load("entry.json", refresh=True)
        assert refreshed == {"a": 2}
        assert cache.snapshot("entry.json").stale is False

    def test_cached_target_is_reused_until_invalidated(self):
        documents = {
            "main.json": {"a": {"$ref": "target.json#/v"}},
            "target.json": {"v": 1},
        }
        calls = []
        cache = JsonLoader(loader_for(documents, calls))
        cache.snapshot("main.json")
        cache.snapshot("main.json")
        assert calls.count("target.json") == 1

        documents["target.json"] = {"v": 2}
        cache.invalidate("target.json")
        cache.snapshot("main.json")
        assert calls.count("target.json") == 2


class TestParseCache(object):
    def test_summary_returns_single_digest(self):
        documents = {
            "main.json": {"a": {"$ref": "target.json#/v"}},
            "target.json": {"v": 1},
        }
        cache = JsonLoader(loader_for(documents))
        cache.snapshot("main.json")
        summary = cache.cache.summary("main.json")
        assert dict(summary)["target.json"] == document_digest(documents["target.json"])
        assert len(summary.digest) == 64

    def test_canonical_uri_strips_fragments(self):
        assert canonical_uri("http://example.com/a.json#/x") == (
            "http://example.com/a.json"
        )

    def test_records_expose_complete_dependency_digest(self):
        record = CacheRecord(
            "main.json",
            {"a": 1},
            dependencies={"target.json": document_digest({"v": 1})},
        )
        assert record.dependency_digest == digest_dependencies(record.dependencies)
        assert record.as_dict()["dependencies"] == record.dependencies

    def test_parse_records_supplied_documents_and_dependencies(self):
        cache = ParseCache()
        documents = {"dictionary.json": {"term": "old"}}
        cache.parse(
            "main.json",
            {"root": {"$ref": "dictionary.json#/term"}},
            ["dictionary.json"],
            loader=documents,
        )
        cache.parse(
            "other.json",
            {"root": {"$ref": "dictionary.json#/term"}},
            {"dictionary.json": document_digest(documents["dictionary.json"])},
        )

        documents["dictionary.json"] = {"term": "new"}
        invalidated = cache.update("dictionary.json", documents["dictionary.json"])

        assert invalidated == {"main.json", "other.json"}
        assert "dictionary.json" in cache
        assert cache["dictionary.json"].digest == document_digest(
            documents["dictionary.json"]
        )

    def test_summary_can_be_reused_for_shared_targets(self):
        cache = ParseCache()
        shared = {"shared.json": {"value": 1}}
        cache.parse("one.json", {"a": 1}, ["shared.json"], loader=shared)
        cache.parse("two.json", {"b": 2}, ["shared.json"], loader=shared)

        cache.invalidate("shared.json")
        # Unchanged target is still available for reuse by either result.
        assert "shared.json" not in cache

        cache.parse("one.json", {"a": 1}, ["shared.json"], loader=shared)
        assert cache["one.json"].dependencies["shared.json"] == document_digest(
            shared["shared.json"]
        )

    def test_loader_records_existing_root_with_transitive_summary(self):
        documents = {
            "dictionary.json": {"term": {"$ref": "detail.json#/detail"}},
            "detail.json": {"detail": "old"},
        }
        cache = JsonLoader(loader_for(documents))
        record = cache.record_document(
            "main.json", {"root": {"$ref": "dictionary.json#/term"}}
        )
        assert set(record.dependencies) == {"dictionary.json", "detail.json"}

        documents["detail.json"] = {"detail": "new"}
        assert cache.cache.update("detail.json", documents["detail.json"]) == {
            "main.json"
        }

    def test_loader_parse_alias_records_dependency_mapping(self):
        cache = JsonLoader(loader_for({}))
        record = cache.parse(
            "main.json",
            {"root": {"$ref": "dictionary.json#/term"}},
            {"dictionary.json": document_digest({"term": "old"})},
        )
        assert record.dependencies["dictionary.json"] == document_digest(
            {"term": "old"}
        )

    def test_cache_is_a_mapping(self):
        cache = ParseCache()
        cache["a.json"] = {"a": 1}
        assert len(cache) == 1
        assert cache["a.json"].document == {"a": 1}
        del cache["a.json"]
        assert "a.json" not in cache

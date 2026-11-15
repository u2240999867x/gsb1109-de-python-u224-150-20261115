import hashlib
import json
import os
from pathlib import Path

import pytest

import jsonref
from jsonref import JsonRefError, replace_refs
from jsonref.loader import (
    AuthorizationError,
    DigestMismatch,
    JsonLoader,
    LoadResult,
    Manifest,
    ManifestError,
    canonicalize_uri,
    load_manifest,
)


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value).encode("utf-8")
    path.write_bytes(payload)
    return path


@pytest.fixture
def batch(tmp_path):
    """A small batch directory with one listed and one unlisted JSON file."""
    root = tmp_path / "batch"
    root.mkdir()
    listed = write_json(root / "listed.json", {"name": "listed"})
    write_json(root / "secret.json", {"name": "secret"})
    return {
        "root": root,
        "listed": listed.read_bytes(),
    }


class TestManifestAuthorization:
    def test_only_manifest_entries_are_readable(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        assert loader("listed.json").document == {"name": "listed"}
        with pytest.raises(AuthorizationError):
            loader("secret.json")

    def test_authorization_happens_before_open(self, batch):
        opened = []

        def opener(uri, **kwargs):
            opened.append(uri)
            return {"name": "listed"}

        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}, opener=opener
        )
        with pytest.raises(AuthorizationError):
            loader("secret.json")
        assert opened == []

    def test_manifest_path_resolves_relative_entries(self, batch):
        manifest_path = batch["root"] / "manifest.json"
        manifest_path.write_text(json.dumps({"files": ["listed.json"]}))
        loader = JsonLoader(manifest_path)
        assert loader("listed.json").document == {"name": "listed"}

    def test_mapping_manifest_entries(self, batch):
        manifest = {
            "root": str(batch["root"]),
            "files": {"listed": "listed.json"},
        }
        assert load_manifest(manifest).is_authorized("listed")

    def test_non_json_manifest_is_rejected(self, tmp_path):
        path = tmp_path / "manifest.json"
        path.write_text("not json")
        with pytest.raises(ManifestError):
            JsonLoader(path)

    def test_empty_manifest_reads_nothing(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": []})
        with pytest.raises(AuthorizationError):
            loader("listed.json")

    def test_manifest_requires_existing_root(self, tmp_path):
        with pytest.raises(ManifestError):
            JsonLoader({"root": str(tmp_path / "missing"), "files": []})


class TestAliases:
    def test_alias_shares_the_loaded_document_object(self, batch):
        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {"listed": {"target": "listed.json", "aliases": ["alias"]}},
            }
        )
        first = loader("listed")
        second = loader("alias")
        assert first.document == second.document
        assert first.document is second.document
        assert second.alias_of == loader.manifest.authorize("listed").key
        assert loader("alias").final_uri == loader("listed").final_uri

    def test_alias_does_not_authorize_the_canonical_name(self, batch):
        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {"alias": {"target": "listed.json"}},
            }
        )
        # The physical target is readable through the alias...
        assert loader("alias").document == {"name": "listed"}
        # ...but the target path itself was not granted a name.
        with pytest.raises(AuthorizationError):
            loader("listed.json")

    def test_alias_does_not_widen_to_unlisted_target(self, batch):
        manifest = {
            "root": str(batch["root"]),
            "aliases": {"alias": "secret.json"},
            "files": {"listed": "listed.json"},
        }
        with pytest.raises(ManifestError):
            JsonLoader(manifest)

    def test_aliases_do_not_grant_directory_reads(self, batch):
        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {"listed": {"target": "listed.json", "aliases": ["copy"]}},
            }
        )
        for candidate in ("", ".", "./", str(batch["root"])):
            with pytest.raises(AuthorizationError):
                loader(candidate)


class TestPathNormalization:
    def test_parent_traversal_is_rejected(self, batch):
        outside = write_json(batch["root"].parent / "outside.json", {"outside": True})
        assert outside.exists()
        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}
        )
        with pytest.raises(AuthorizationError):
            loader("../outside.json")

    def test_encoded_traversal_is_rejected(self, batch):
        write_json(batch["root"].parent / "outside.json", {"outside": True})
        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}
        )
        for candidate in (
            "%2e%2e/outside.json",
            "..%2foutside.json",
            "%252e%252e/outside.json",
            "listed.json/../../outside.json",
            "./../outside.json",
        ):
            with pytest.raises(AuthorizationError):
                loader(candidate)

    def test_absolute_path_outside_root_is_rejected(self, batch):
        outside = write_json(batch["root"].parent / "outside.json", {"outside": True})
        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}
        )
        with pytest.raises(AuthorizationError):
            loader(str(outside))

    def test_symlink_escape_is_rejected(self, batch):
        outside = write_json(batch["root"].parent / "outside.json", {"outside": True})
        link = batch["root"] / "link.json"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
        manifest = {"root": str(batch["root"]), "files": ["link.json"]}
        with pytest.raises(AuthorizationError):
            JsonLoader(manifest)

    def test_normalized_spelling_matches_the_listed_name(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        for candidate in (
            "./listed.json",
            "sub/../listed.json",
            str(batch["root"] / "listed.json"),
            (batch["root"] / "listed.json").as_uri(),
        ):
            assert loader(candidate).document == {"name": "listed"}

    def test_canonicalize_uri_normalizes_percent_escapes(self, batch):
        uri = canonicalize_uri("%6Cisted.json", root=batch["root"])
        assert uri.endswith("/listed.json")

    def test_canonicalize_uri_rejects_encoded_separator(self, batch):
        with pytest.raises(AuthorizationError):
            canonicalize_uri("sub%2F..%2Foutside.json", root=batch["root"])


class TestDigestVerification:
    def test_matching_digest_is_recorded(self, batch):
        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed": {
                        "target": "listed.json",
                        "sha256": digest(batch["listed"]),
                    }
                },
            }
        )
        result = loader("listed")
        assert result.digest == digest(batch["listed"])
        assert loader.last_digest == digest(batch["listed"])

    def test_mismatching_digest_is_rejected(self, batch):
        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed": {"target": "listed.json", "sha256": "0" * 64}
                },
            }
        )
        with pytest.raises(DigestMismatch):
            loader("listed")

    def test_malformed_digest_is_rejected(self, batch):
        with pytest.raises(ManifestError):
            JsonLoader(
                {
                    "root": str(batch["root"]),
                    "files": {"listed": {"target": "listed.json", "sha256": "abc"}},
                }
            )


class TestRedirectAuthorization:
    def test_authorized_redirect_final_address_is_used(self, batch):
        def opener(uri, **kwargs):
            return LoadResult({"name": "listed"}, "listed-copy.json", None)

        manifest = {
            "root": str(batch["root"]),
            "files": {
                "listed": {
                    "target": "listed.json",
                    "redirects": ["listed-copy.json"],
                }
            },
        }
        loader = JsonLoader(manifest, opener=opener)
        result = loader("listed")
        assert result.final_uri.endswith("/listed-copy.json")
        assert loader.last_source == result.final_uri
        record = loader.last_record
        assert record.digest == result.digest
        assert record.requested_uri == loader.manifest.authorize("listed").key
        assert record.authorized_uri == loader.manifest.authorize("listed").key

    def test_unauthorized_redirect_is_rejected(self, batch):
        def opener(uri, **kwargs):
            return LoadResult({"name": "secret"}, "secret.json", None)

        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}, opener=opener
        )
        with pytest.raises(AuthorizationError):
            loader("listed")

    def test_redirect_to_another_listed_file_is_authorized(self, batch):
        other = write_json(batch["root"] / "other.json", {"name": "other"})

        def opener(uri, **kwargs):
            return LoadResult({"name": "other"}, "other.json", digest(other.read_bytes()))

        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json", "other.json"]},
            opener=opener,
        )
        result = loader("listed.json")
        assert result.final_uri.endswith("/other.json")
        assert result.digest == digest(other.read_bytes())

    def test_redirect_to_another_listed_file_uses_its_digest(self, batch):
        other = write_json(batch["root"] / "other.json", {"name": "other"})

        def opener(uri, **kwargs):
            return LoadResult({"name": "other"}, "other.json", digest(other.read_bytes()))

        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed.json": "listed.json",
                    "other.json": {
                        "target": "other.json",
                        "sha256": digest(other.read_bytes()),
                    },
                },
            },
            opener=opener,
        )
        assert loader("listed.json").final_uri.endswith("/other.json")

    def test_redirect_to_unlisted_file_is_rejected(self, batch):
        def opener(uri, **kwargs):
            return LoadResult({"name": "secret"}, "secret.json", None)

        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}, opener=opener
        )
        with pytest.raises(AuthorizationError):
            loader("listed.json")

    def test_redirect_to_alias_of_the_same_content_is_allowed(self, batch):
        def opener(uri, **kwargs):
            return LoadResult({"name": "listed"}, "alias.json", None)

        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed": {
                        "target": "listed.json",
                        "aliases": ["alias.json"],
                    }
                },
            },
            opener=opener,
        )
        assert loader("listed").final_uri.endswith("/alias.json")

    def test_http_redirect_requires_declared_destination(self):
        digest_value = hashlib.sha256(b'{"a": 1}').hexdigest()

        def opener(uri, **kwargs):
            return LoadResult({"a": 1}, "https://cdn.example/a.json", digest_value)

        manifest = {
            "base_uri": "https://example.test/",
            "files": {
                "https://example.test/a.json": {
                    "target": "https://example.test/a.json",
                    "sha256": digest_value,
                }
            },
        }
        loader = JsonLoader(manifest, opener=opener)
        with pytest.raises(AuthorizationError):
            loader("https://example.test/a.json")

    def test_http_relative_manifest_entries_use_the_base(self):
        def opener(uri, **kwargs):
            assert uri == "https://example.test/a.json"
            return LoadResult({"a": 1}, uri, None)

        loader = JsonLoader(
            {"base_uri": "https://example.test/", "files": ["a.json"]},
            opener=opener,
        )
        assert loader("a.json").document == {"a": 1}

    def test_redirect_chain_must_be_authorized_hop_by_hop(self):
        def opener(uri, **kwargs):
            return LoadResult(
                {"a": 1},
                "https://cdn.example/final.json",
                None,
                redirect_chain=[
                    "https://cdn.example/one.json",
                    "https://cdn.example/final.json",
                ],
            )

        manifest = {
            "base_uri": "https://example.test/",
            "files": {
                "https://example.test/a.json": {
                    "target": "https://example.test/a.json",
                    "redirects": ["https://cdn.example/one.json"],
                }
            },
        }
        loader = JsonLoader(manifest, opener=opener)
        with pytest.raises(AuthorizationError):
            loader("https://example.test/a.json")

    def test_fully_authorized_redirect_chain_is_accepted(self):
        def opener(uri, **kwargs):
            return LoadResult(
                {"a": 1},
                "https://cdn.example/final.json",
                None,
                redirect_chain=[
                    "https://cdn.example/one.json",
                    "https://cdn.example/final.json",
                ],
            )

        manifest = {
            "base_uri": "https://example.test/",
            "files": {
                "https://example.test/a.json": {
                    "target": "https://example.test/a.json",
                    "redirects": [
                        "https://cdn.example/one.json",
                        "https://cdn.example/final.json",
                    ],
                }
            },
        }
        loader = JsonLoader(manifest, opener=opener)
        result = loader("https://example.test/a.json")
        assert result.final_uri == "https://cdn.example/final.json"
        assert result.redirect_chain == (
            "https://cdn.example/one.json",
            "https://cdn.example/final.json",
        )
        assert loader.last_record.redirect_chain == result.redirect_chain

    def test_shared_target_uses_each_entry_redirect_policy(self, batch):
        def opener(uri, **kwargs):
            return LoadResult({"name": "listed"}, "listed-copy.json", None)

        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed": {
                        "target": "listed.json",
                        "redirects": ["listed-copy.json"],
                    },
                    "alias_entry": {"target": "listed.json"},
                },
            },
            opener=opener,
        )
        assert loader("listed").final_uri.endswith("/listed-copy.json")
        # The second entry shares the physical target but did not declare the
        # redirect, so it must not silently inherit the first entry's result.
        with pytest.raises(AuthorizationError):
            loader("alias_entry")

    def test_cached_redirect_chain_is_reauthorized(self, batch):
        calls = []

        def opener(uri, **kwargs):
            calls.append(uri)
            return LoadResult(
                {"name": "listed"},
                "listed-copy.json",
                None,
                redirect_chain=["listed-copy.json"],
            )

        loader = JsonLoader(
            {
                "root": str(batch["root"]),
                "files": {
                    "listed": {
                        "target": "listed.json",
                        "redirects": ["listed-copy.json"],
                    }
                },
            },
            opener=opener,
        )
        loader("listed")
        second = loader("listed")
        assert len(calls) == 1
        assert second.final_uri.endswith("/listed-copy.json")
        assert loader.last_record.from_cache is True
        assert loader.last_record.redirect_chain == ("listed-copy.json",)


class TestJsonRefIntegration:
    def test_proxy_exposes_final_source_and_digest(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        document = {"a": {"$ref": "listed.json"}}
        result = replace_refs(
            document, base_uri=str(batch["root"]) + "/", loader=loader
        )
        assert result["a"] == {"name": "listed"}
        assert result["a"].final_uri == result["a"].source_uri
        assert result["a"].digest == digest(batch["listed"])
        assert result["a"].load_record.final_uri == result["a"].final_uri

    def test_shared_store_refs_keep_their_audit_record(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        document = {
            "a": {"$ref": "listed.json"},
            "b": {"$ref": "listed.json"},
        }
        result = replace_refs(
            document, base_uri=str(batch["root"]) + "/", loader=loader
        )
        assert result["a"].__subject__ is result["b"].__subject__
        assert len(loader.records) == 1
        first = result["a"].load_record
        second = result["b"].load_record
        assert first is not None and second is not None
        assert first.final_uri == second.final_uri
        assert first.digest == second.digest

    def test_proxy_rejects_unlisted_reference(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        document = {"a": {"$ref": "secret.json"}}
        result = replace_refs(
            document, base_uri=str(batch["root"]) + "/", loader=loader
        )
        with pytest.raises(JsonRefError) as excinfo:
            result["a"].__subject__
        assert isinstance(excinfo.value.cause, AuthorizationError)

    def test_proxy_uses_redirected_base_for_child_references(self, batch):
        write_json(batch["root"] / "nested.json", {"nested": True})

        def opener(uri, **kwargs):
            if uri.endswith("/entry.json"):
                return LoadResult(
                    {"child": {"$ref": "nested.json"}},
                    "https://files.example/entry.json",
                    None,
                )
            if uri == "https://files.example/nested.json":
                return LoadResult({"nested": True}, uri, None)
            raise AssertionError(uri)

        manifest = {
            "base_uri": "https://files.example/",
            "files": {
                "https://files.example/entry.json": {
                    "target": "https://files.example/entry.json",
                    "redirects": ["https://files.example/entry.json"],
                },
                "https://files.example/nested.json": (
                    "https://files.example/nested.json"
                ),
            },
        }
        loader = JsonLoader(manifest, opener=opener)
        result = replace_refs(
            {"entry": {"$ref": "https://files.example/entry.json"}}, loader=loader
        )
        assert result["entry"]["child"] == {"nested": True}

    def test_custom_loader_redirect_is_rejected(self):
        class RedirectingLoader:
            def __call__(self, uri):
                return LoadResult({"a": 1}, "elsewhere.json", None)

        result = replace_refs({"a": {"$ref": "start.json"}}, loader=RedirectingLoader())
        with pytest.raises(JsonRefError) as excinfo:
            result["a"].__subject__
        assert "unauthorized" in str(excinfo.value)

    def test_dump_and_dumps_keep_the_reference(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        document = {"a": {"$ref": "listed.json"}}
        result = replace_refs(
            document, base_uri=str(batch["root"]) + "/", loader=loader
        )
        assert jsonref.dumps(result) == json.dumps(document)


class TestPublicApi:
    def test_loader_is_exported(self):
        assert jsonref.JsonLoader is JsonLoader
        assert jsonref.manifest_loader is not None

    def test_manifest_loader_helper(self, batch):
        loader = jsonref.manifest_loader(
            {"root": str(batch["root"]), "files": ["listed.json"]}
        )
        assert loader("listed.json").document == {"name": "listed"}

    def test_manifest_can_be_converted_from_directory(self, batch):
        manifest = Manifest.from_directory(batch["root"])
        assert manifest.is_authorized("listed.json")

    def test_loader_records_are_immutable_and_ordered(self, batch):
        loader = JsonLoader({"root": str(batch["root"]), "files": ["listed.json"]})
        loader("listed.json")
        loader("listed.json")
        assert len(loader.records) == 2
        assert loader.records[0].from_cache is False
        assert loader.records[1].from_cache is True
        assert loader.records[0].digest == loader.records[1].digest
        with pytest.raises(AttributeError):
            loader.records[0].digest = "changed"

    def test_concurrent_misses_open_the_target_once(self, batch):
        import threading

        calls = []

        def opener(uri, **kwargs):
            calls.append(uri)
            return {"name": "listed"}

        loader = JsonLoader(
            {"root": str(batch["root"]), "files": ["listed.json"]}, opener=opener
        )
        errors = []

        def work():
            try:
                loader("listed.json")
            except Exception as exc:  # pragma: no cover - failure diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert errors == []
        assert len(calls) == 1
        assert len(loader.records) == 8
        assert sum(record.from_cache for record in loader.records) == 7

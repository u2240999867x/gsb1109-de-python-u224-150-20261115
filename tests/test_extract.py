import json
import posixpath
from urllib import parse as urlparse

import pytest

import jsonref
from jsonref import ExtractionError, extract_subtree


def resolve_package(result):
    """Resolve every document of an exported package as plain Python data."""

    def loader(uri):
        path = urlparse.urlsplit(uri).path
        relative = posixpath.relpath(path, start="/pkg/")
        return result.documents[relative]

    resolved = {}
    for name, data in result.documents.items():
        base_uri = "file:///pkg/" + name
        resolved[name] = jsonref.replace_refs(
            data, base_uri=base_uri, loader=loader, proxies=False, lazy_load=False
        )
    return resolved


class TestMinimalExtraction(object):
    def test_selects_only_subtree_and_dependencies(self):
        document = {
            "wanted": {
                "value": 1,
                "shared": {"$ref": "#/definitions/shared"},
            },
            "unwanted": {"huge": [1, 2, 3], "ref": {"$ref": "#/definitions/shared"}},
            "definitions": {
                "shared": {"kind": "shared"},
                "orphan": {"kind": "never referenced"},
            },
        }
        result = extract_subtree(document, "#/wanted")
        assert result.self_contained
        root = result.documents[result.root_document]

        assert set(root.keys()) == {"wanted", "definitions"}
        assert root["wanted"] == {
            "value": 1,
            "shared": {"$ref": "#/definitions/shared"},
        }
        assert root["definitions"] == {"shared": {"kind": "shared"}}
        assert "orphan" not in root["definitions"]
        assert "unwanted" not in root

    def test_root_pointer_recorded(self):
        document = {"a": {"b": 1}}
        result = extract_subtree(document, "/a/b")
        assert result.root_document == "root.json"
        assert result.root_pointer == "#/a/b"

    def test_whole_document_pointer(self):
        document = {"a": 1}
        assert extract_subtree(document, "").root_pointer == "#"
        assert extract_subtree(document, "#").root_pointer == "#"

    def test_invalid_root_pointer_raises(self):
        with pytest.raises(ExtractionError):
            extract_subtree({"a": 1}, "#/missing")

    def test_shared_reference_stays_a_reference(self):
        document = {
            "target": {"name": "thing"},
            "pick": {
                "one": {"$ref": "#/target"},
                "two": {"$ref": "#/target"},
            },
        }
        result = extract_subtree(document, "#/pick")
        root = result.documents[result.root_document]
        assert root["pick"]["one"] == {"$ref": "#/target"}
        assert root["pick"]["two"] == {"$ref": "#/target"}

        resolved = resolve_package(result)[result.root_document]
        assert resolved["pick"]["one"] is resolved["pick"]["two"]
        assert resolved["pick"]["one"] == {"name": "thing"}

    def test_recursive_reference_is_preserved(self):
        document = {
            "node": {
                "name": "root",
                "children": [{"$ref": "#/node"}],
            }
        }
        result = extract_subtree(document, "#/node")
        root = result.documents[result.root_document]
        assert root["node"]["children"] == [{"$ref": "#/node"}]

        resolved = resolve_package(result)[result.root_document]
        assert resolved["node"]["children"][0] is resolved["node"]

    def test_escaped_pointer_keys(self):
        document = {"a/~a": {"keep": True}, "pick": {"$ref": "#/a~1~0a"}}
        result = extract_subtree(document, "#/pick")
        root = result.documents[result.root_document]
        assert root["pick"] == {"$ref": "#/a~1~0a"}
        assert "a/~a" in root
        assert resolve_package(result)[result.root_document]["pick"] == {"keep": True}

    def test_array_index_keeps_whole_ancestor_array(self):
        document = {
            "items": [
                {"id": 0},
                {"id": 1, "link": {"$ref": "#/shared"}},
            ],
            "shared": {"value": True},
            "dropped": {"sibling": True},
        }
        result = extract_subtree(document, "#/items/1")
        root = result.documents[result.root_document]
        # The array is kept intact so the root pointer still resolves by index.
        assert [item["id"] for item in root["items"]] == [0, 1]
        assert root["items"][1]["link"] == {"$ref": "#/shared"}
        assert "dropped" not in root
        resolved = resolve_package(result)[result.root_document]
        assert resolved["items"][1]["link"] == {"value": True}

    def test_jsonschema_id_updates_reference_base(self):
        documents = {
            "http://example.com/schemas/other.json": {"x": {"ok": True}},
        }
        document = {
            "$id": "http://example.com/schemas/main",
            "link": {"$ref": "other.json#/x"},
        }
        result = extract_subtree(
            document,
            "#",
            base_uri="file:///src/main.json",
            loader=documents.get,
            jsonschema=True,
        )
        assert result.self_contained
        root = result.documents[result.root_document]
        assert root["link"] == {"$ref": "other.json#/x"}
        assert resolve_package(result)[result.root_document]["link"] == {"ok": True}


class TestCrossDocument(object):
    def test_relative_reference_rewritten_to_package_path(self):
        documents = {
            "file:///src/main.json": {
                "detail": {"$ref": "other.json#/pieces/one"},
            },
            "file:///src/other.json": {
                "pieces": {"one": {"id": 1}, "dropped": {"id": 2}},
            },
        }
        result = extract_subtree(
            documents["file:///src/main.json"],
            "#",
            base_uri="file:///src/main.json",
            loader=documents.get,
        )
        assert result.self_contained
        root = result.documents["root.json"]
        assert root["detail"] == {"$ref": "other.json#/pieces/one"}
        other = result.documents["other.json"]
        assert other == {"pieces": {"one": {"id": 1}}}
        assert "dropped" not in other["pieces"]

    def test_nested_relative_paths_flatten_to_package_local_names(self):
        documents = {
            "file:///src/a/main.json": {"ref": {"$ref": "../b/other.json"}},
            "file:///src/b/other.json": {"value": 42},
        }
        result = extract_subtree(
            documents["file:///src/a/main.json"],
            "#",
            base_uri="file:///src/a/main.json",
            loader=documents.get,
        )
        root = result.documents["root.json"]
        assert root["ref"] == {"$ref": "other.json"}
        assert result.documents["other.json"] == {"value": 42}

    def test_transitive_documents_are_included(self):
        documents = {
            "a.json": {"next": {"$ref": "b.json"}},
            "b.json": {"next": {"$ref": "c.json"}},
            "c.json": {"end": True},
        }
        result = extract_subtree(
            documents["a.json"], "#", base_uri="a.json", loader=documents.get
        )
        assert set(result.documents) == {"root.json", "b.json", "c.json"}
        resolved = resolve_package(result)
        assert resolved["root.json"]["next"]["next"] == {"end": True}

    def test_unreferenced_sibling_document_is_not_loaded(self):
        loaded = []

        def loader(uri):
            loaded.append(uri)
            return {
                "a.json": {"used": {"$ref": "b.json"}},
                "b.json": {"ok": True},
                "c.json": {"never": True},
            }[uri]

        extract_subtree(
            {"used": {"$ref": "b.json"}},
            "#",
            base_uri="a.json",
            loader=loader,
        )
        assert "c.json" not in loaded

    def test_cross_document_cycle(self):
        documents = {
            "file:///json1": {"a": {"$ref": "/json2"}},
            "file:///json2": {"b": {"$ref": "/json1"}},
        }
        result = extract_subtree(
            documents["file:///json1"],
            "#",
            base_uri="file:///json1",
            loader=documents.get,
        )
        assert result.self_contained
        names = result.documents
        root_name = result.root_document
        other_name = next(name for name in names if name != root_name)
        assert names[root_name]["a"] == {"$ref": other_name}
        assert names[other_name]["b"] == {"$ref": root_name}

        resolved = resolve_package(result)
        assert resolved[root_name]["a"]["b"] is resolved[root_name]


class TestMissingDependencies(object):
    def test_missing_document_reports_full_chain(self):
        documents = {
            "a.json": {"step": {"$ref": "b.json#/x"}},
            "b.json": {"x": {"step": {"$ref": "c.json#/y"}}},
        }

        def loader(uri):
            if uri == "c.json":
                raise FileNotFoundError("nope")
            return documents[uri]

        result = extract_subtree(
            documents["a.json"], "#", base_uri="a.json", loader=loader
        )
        assert not result.self_contained
        missing = result.missing
        assert len(missing) == 1
        record = missing[0]
        assert record["uri"] == "c.json"
        assert "FileNotFoundError" in record["reason"]
        chain = record["chain"]
        assert [hop["ref"] for hop in chain] == ["b.json#/x", "c.json#/y"]
        assert chain[0]["document"] == "a.json"
        assert chain[1]["document"] == "b.json"

    def test_unresolvable_fragment_reports_chain(self):
        documents = {
            "a.json": {"link": {"$ref": "b.json#/gone"}},
            "b.json": {"present": 1},
        }
        result = extract_subtree(
            documents["a.json"], "#", base_uri="a.json", loader=documents.get
        )
        assert not result.self_contained
        record = result.missing[0]
        assert record["uri"] == "b.json#/gone"
        assert record["chain"][0]["ref"] == "b.json#/gone"

    def test_broken_ref_rewritten_to_absolute_uri_not_relative(self):
        documents = {"a.json": {"link": {"$ref": "missing.json"}}}
        result = extract_subtree(
            documents["a.json"],
            "#",
            base_uri="file:///src/a.json",
            loader=lambda uri: (_ for _ in ()).throw(FileNotFoundError(uri)),
        )
        root = result.documents[result.root_document]
        assert root["link"] == {"$ref": "file:///src/missing.json"}

    def test_package_never_pretends_self_contained(self):
        result = extract_subtree(
            {"link": {"$ref": "missing.json"}},
            "#",
            base_uri="a.json",
            loader=lambda uri: (_ for _ in ()).throw(RuntimeError("down")),
        )
        assert result.self_contained is False
        manifest = result.to_manifest()
        assert manifest["self_contained"] is False
        assert manifest["unresolved"]


class TestWriteTo(object):
    def test_writes_documents_and_manifest(self, tmp_path):
        documents = {
            "file:///src/main.json": {"ref": {"$ref": "other.json#/x"}},
            "file:///src/other.json": {"x": {"ok": True}},
        }
        result = extract_subtree(
            documents["file:///src/main.json"],
            "#/ref",
            base_uri="file:///src/main.json",
            loader=documents.get,
        )
        manifest = result.write_to(str(tmp_path))

        assert json.loads((tmp_path / "root.json").read_text()) == result.documents[
            "root.json"
        ]
        assert json.loads((tmp_path / "other.json").read_text()) == {
            "x": {"ok": True}
        }
        on_disk = json.loads((tmp_path / "manifest.json").read_text())
        assert on_disk == manifest
        assert manifest["root"] == {"document": "root.json", "pointer": "#/ref"}
        assert manifest["self_contained"] is True

    def test_flat_output_paths(self, tmp_path):
        documents = {
            "file:///src/a/main.json": {"ref": {"$ref": "../b/other.json"}},
            "file:///src/b/other.json": {"value": 1},
        }
        result = extract_subtree(
            documents["file:///src/a/main.json"],
            "#",
            base_uri="file:///src/a/main.json",
            loader=documents.get,
        )
        result.write_to(str(tmp_path))
        assert (tmp_path / "root.json").exists()
        assert (tmp_path / "other.json").exists()
        assert not any(path.is_dir() for path in tmp_path.iterdir())

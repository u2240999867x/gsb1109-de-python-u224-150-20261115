import pytest

from jsonref import ReferenceGraph, replace_refs


def make_loader(documents):
    calls = []

    def loader(uri):
        calls.append(uri)
        return documents[uri]

    return loader, calls


def test_same_relative_ref_in_two_directories():
    documents = {
        "pkg/index.json": {
            "items": [{"$ref": "a/config.json"}, {"$ref": "b/config.json"}]
        },
        "pkg/a/config.json": {"name": "a", "shared": {"$ref": "shared.json"}},
        "pkg/b/config.json": {"name": "b", "shared": {"$ref": "shared.json"}},
        "pkg/a/shared.json": {"where": "a"},
        "pkg/b/shared.json": {"where": "b"},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        return_graph=True,
    )

    assert result == {
        "items": [
            {"name": "a", "shared": {"where": "a"}},
            {"name": "b", "shared": {"where": "b"}},
        ]
    }
    assert calls == [
        "pkg/a/config.json",
        "pkg/a/shared.json",
        "pkg/b/config.json",
        "pkg/b/shared.json",
    ]

    shared_refs = [ref for ref in graph if ref.ref == "shared.json"]
    assert [ref.full_uri for ref in shared_refs] == [
        "pkg/a/shared.json",
        "pkg/b/shared.json",
    ]
    assert shared_refs[0].source.base_uri == "pkg/a/config.json"
    assert shared_refs[1].source.base_uri == "pkg/b/config.json"


def test_shared_target_loaded_once_but_both_paths_retained():
    documents = {
        "pkg/index.json": {
            "a": {"$ref": "a/config.json"},
            "b": {"$ref": "b/config.json"},
        },
        "pkg/a/config.json": {"shared": {"$ref": "../common/shared.json"}},
        "pkg/b/config.json": {"shared": {"$ref": "../common/shared.json"}},
        "pkg/common/shared.json": {"shared": True},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        return_graph=True,
    )

    assert result["a"]["shared"] == result["b"]["shared"] == {"shared": True}
    assert calls.count("pkg/common/shared.json") == 1

    target = graph.find_document("pkg/common/shared.json")
    assert target is not None
    paths = graph.paths_to(target)
    assert len(paths) == 2
    assert [
        [reference.full_uri for reference in path] for path in paths
    ] == [
        ["pkg/a/config.json", "pkg/common/shared.json"],
        ["pkg/b/config.json", "pkg/common/shared.json"],
    ]

    refs = graph.references_to("pkg/common/shared.json")
    assert len(refs) == 2
    assert {ref.source.base_uri for ref in refs} == {
        "pkg/a/config.json",
        "pkg/b/config.json",
    }
    assert target.incoming == refs


def test_reference_metadata_survives_proxy_loading():
    documents = {
        "pkg/root.json": {"config": {"$ref": "config.json"}},
        "pkg/config.json": {"value": 1},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/root.json"],
        base_uri="pkg/root.json",
        loader=loader,
        return_graph=True,
        load_on_repr=False,
    )

    ref = result["config"]
    assert ref.base_uri == "pkg/root.json"
    assert ref.source_uri == "pkg/root.json"
    assert ref.path == ("config",)
    assert ref.full_uri == "pkg/config.json"
    assert [item.full_uri for item in ref.chain] == ["pkg/config.json"]

    assert ref.__subject__ == {"value": 1}

    assert ref.base_uri == "pkg/root.json"
    assert ref.source_uri == "pkg/root.json"
    assert ref.path == ("config",)
    assert ref.target is graph.find_document("pkg/config.json")
    assert [item.full_uri for item in ref.chain] == ["pkg/config.json"]


def test_declared_id_rebases_references():
    documents = {
        "pkg/index.json": {
            "a": {"$ref": "a.json"},
            "b": {"$ref": "b.json"},
        },
        "pkg/a.json": {"$id": "https://example.com/a/", "c": {"$ref": "c.json"}},
        "pkg/b.json": {"$id": "https://example.com/b/", "c": {"$ref": "c.json"}},
        "https://example.com/a/c.json": {"where": "a"},
        "https://example.com/b/c.json": {"where": "b"},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        jsonschema=True,
        return_graph=True,
    )

    assert result == {
        "a": {"$id": "https://example.com/a/", "c": {"where": "a"}},
        "b": {"$id": "https://example.com/b/", "c": {"where": "b"}},
    }

    a_document = graph.find_document("pkg/a.json")
    b_document = graph.find_document("pkg/b.json")
    assert a_document.retrieval_uri == "pkg/a.json"
    assert a_document.base_uri == "https://example.com/a/"
    assert b_document.retrieval_uri == "pkg/b.json"
    assert b_document.base_uri == "https://example.com/b/"

    assert [ref.full_uri for ref in a_document.references] == [
        "https://example.com/a/c.json"
    ]
    assert [ref.full_uri for ref in b_document.references] == [
        "https://example.com/b/c.json"
    ]


def test_nested_id_base_is_used_for_children():
    documents = {
        "pkg/index.json": {
            "outer": {"$id": "sub/", "inner": {"$ref": "thing.json"}}
        },
        "pkg/sub/thing.json": {"value": 1},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        jsonschema=True,
        return_graph=True,
    )

    assert result == {"outer": {"$id": "sub/", "inner": {"value": 1}}}
    assert calls == ["pkg/sub/thing.json"]

    resource = graph.find_document("pkg/sub/")
    assert resource is not None
    assert resource.retrieval_uri == "pkg/index.json"
    assert resource.base_uri == "pkg/sub/"
    assert resource.declared_id == "sub/"
    assert resource.container.key == graph.root.key


def test_cache_key_retains_base_for_same_retrieval_uri():
    documents = {
        "pkg/index.json": {
            "x": {"$ref": "a.json"},
            "y": {"$ref": "https://example.com/other/"},
        },
        "pkg/a.json": {"$id": "https://example.com/other/", "v": 1},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        jsonschema=True,
        return_graph=True,
    )

    assert result == {
        "x": {"$id": "https://example.com/other/", "v": 1},
        "y": {"$id": "https://example.com/other/", "v": 1},
    }
    # The document is only fetched once, through its retrieval URI.
    assert calls == ["pkg/a.json"]

    store = result["x"].store
    retrieval_key = store.canonical_key("pkg/a.json")
    base_key = store.canonical_key("https://example.com/other/")
    assert retrieval_key == base_key
    assert retrieval_key == ("pkg/a.json", "https://example.com/other/")

    graph_keys = list(graph.documents)
    assert graph_keys == [
        ("pkg/index.json", "pkg/index.json"),
        ("pkg/a.json", "https://example.com/other/"),
    ]


def test_duplicate_declared_ids_keep_separate_bases():
    documents = {
        "pkg/index.json": {
            "a": {"$ref": "a/config.json"},
            "b": {"$ref": "b/config.json"},
        },
        "pkg/a/config.json": {"$id": "config", "value": {"$ref": "value.json"}},
        "pkg/b/config.json": {"$id": "config", "value": {"$ref": "value.json"}},
        "pkg/a/value.json": {"where": "a"},
        "pkg/b/value.json": {"where": "b"},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        jsonschema=True,
        return_graph=True,
    )

    assert result == {
        "a": {"$id": "config", "value": {"where": "a"}},
        "b": {"$id": "config", "value": {"where": "b"}},
    }
    assert calls == [
        "pkg/a/config.json",
        "pkg/a/value.json",
        "pkg/b/config.json",
        "pkg/b/value.json",
    ]


def test_graph_serialization():
    documents = {
        "pkg/index.json": {"a": {"$ref": "a.json"}},
        "pkg/a.json": {"v": 1},
    }
    loader, calls = make_loader(documents)

    result, graph = replace_refs(
        documents["pkg/index.json"],
        base_uri="pkg/index.json",
        loader=loader,
        return_graph=True,
    )

    data = graph.to_dict()
    assert data["nodes"] == [
        {
            "index": 0,
            "key": ["pkg/index.json", "pkg/index.json"],
            "retrieval_uri": "pkg/index.json",
            "base_uri": "pkg/index.json",
            "id": None,
            "declared_id": None,
            "container": None,
            "parents": [],
            "incoming": [],
            "paths": [[]],
            "references": [0],
        },
        {
            "index": 1,
            "key": ["pkg/a.json", "pkg/a.json"],
            "retrieval_uri": "pkg/a.json",
            "base_uri": "pkg/a.json",
            "id": None,
            "declared_id": None,
            "container": 0,
            "parents": [{"document": 0, "reference": 0}],
            "incoming": [0],
            "paths": [[0]],
            "references": [],
        },
    ]
    assert data["edges"] == [
        {
            "source": 0,
            "source_uri": "pkg/index.json",
            "target": 1,
            "path": ["a"],
            "reference": {"$ref": "a.json"},
            "ref": "a.json",
            "uri": "pkg/a.json",
            "pointer": "",
            "full_uri": "pkg/a.json",
            "resolved": True,
            "chain": [0],
            "chains": [[0]],
        }
    ]
    assert graph.to_json() == (
        '{"nodes": [{"index": 0, "key": ["pkg/index.json", "pkg/index.json"], '
        '"retrieval_uri": "pkg/index.json", "base_uri": "pkg/index.json", '
        '"id": null, "declared_id": null, "container": null, "parents": [], '
        '"incoming": [], "paths": [[]], "references": [0]}, {"index": 1, '
        '"key": ["pkg/a.json", "pkg/a.json"], "retrieval_uri": "pkg/a.json", '
        '"base_uri": "pkg/a.json", "id": null, "declared_id": null, '
        '"container": 0, "parents": [{"document": 0, "reference": 0}], '
        '"incoming": [0], "paths": [[0]], "references": []}], "edges": [{"source": 0, '
        '"source_uri": "pkg/index.json", "target": 1, "path": ["a"], '
        '"reference": {"$ref": "a.json"}, "ref": "a.json", "uri": "pkg/a.json", '
        '"pointer": "", "full_uri": "pkg/a.json", "resolved": true, "chain": [0], '
        '"chains": [[0]]}]}'
    )
    mermaid = graph.to_mermaid()
    assert mermaid.splitlines() == [
        "flowchart LR",
        '    document_0["pkg/index.json"]',
        '    document_1["pkg/a.json"]',
        '    document_0 -->|"a.json"| document_1',
    ]


def test_graph_type_is_exported():
    result, graph = replace_refs({"a": 1}, return_graph=True)
    assert isinstance(graph, ReferenceGraph)
    assert result == {"a": 1}

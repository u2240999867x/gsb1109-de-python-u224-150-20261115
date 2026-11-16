import json
from pathlib import Path

import pytest

from jsonref.merge import (
    MISSING,
    MergeConflict,
    MergeError,
    MergeResult,
    ReferenceResolver,
    analyse_references,
    merge3,
    merge_directories,
    merge_files,
    three_way_merge,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")
    return path


def kinds(result):
    return [conflict.kind for conflict in result.conflicts]


class TestReferenceResolver:
    def test_local_pointer(self):
        document = {"a": {"b": [1, 2, 3]}}
        resolver = ReferenceResolver({"d.json": document}, "d.json")
        assert resolver.dereference({"$ref": "#/a/b/1"}) == 2

    def test_escaped_pointer(self):
        document = {"a/b": {"~x": 7}}
        resolver = ReferenceResolver({"d.json": document}, "d.json")
        assert resolver.dereference({"$ref": "#/a~1b/~0x"}) == 7

    def test_cross_file_relative_reference(self):
        documents = {
            "config/base.json": {"shared": {"limit": 1}},
            "config/app.json": {"ref": {"$ref": "base.json#/shared"}},
        }
        resolver = ReferenceResolver(documents, "config/app.json")
        assert resolver.dereference({"$ref": "base.json#/shared"}) == {"limit": 1}

    def test_recursive_chain_terminates(self):
        documents = {
            "a.json": {"x": {"$ref": "b.json#/y"}},
            "b.json": {"y": {"$ref": "a.json#/x"}},
        }
        resolver = ReferenceResolver(documents, "a.json")
        assert resolver.dereference({"$ref": "b.json#/y"}) is not None
        chain = resolver.chain("b.json#/y", "a.json")
        assert any(entry.get("cycle") for entry in chain)

    def test_self_reference_is_a_cycle(self):
        documents = {"a.json": {"x": {"$ref": "#/x"}}}
        resolver = ReferenceResolver(documents, "a.json")
        chain = resolver.chain("#/x", "a.json")
        assert chain[-1].get("cycle") is True

    def test_unresolved_target(self):
        resolver = ReferenceResolver({"a.json": {"x": 1}}, "a.json")
        assert resolver.dereference({"$ref": "#/missing"}) is not None
        assert resolver.chain("#/missing", "a.json")[-1].get("unresolved")


class TestOrdinaryMerge:
    def test_disjoint_object_edits_are_kept(self):
        base = {"a": 1, "b": {"x": 1, "y": 2}}
        ours = {"a": 2, "b": {"x": 1, "y": 2}}
        theirs = {"a": 1, "b": {"x": 1, "y": 3}}
        result = merge3(base, ours, theirs)
        assert result.merged == {"a": 2, "b": {"x": 1, "y": 3}}
        assert result.clean

    def test_same_value_on_both_sides_is_clean(self):
        base = {"a": 1}
        changed = {"a": 2}
        result = merge3(base, changed, changed)
        assert result.merged == {"a": 2}
        assert result.clean

    def test_same_value_but_independent_objects_stay_independent(self):
        base = {"left": {"v": 1}, "right": {"v": 1}}
        ours = {"left": {"v": 2}, "right": {"v": 1}}
        theirs = {"left": {"v": 1}, "right": {"v": 2}}
        result = merge3(base, ours, theirs)
        assert result.merged == {"left": {"v": 2}, "right": {"v": 2}}
        assert result.clean
        assert not result.analysis["shared_target_updates"]

    def test_true_conflict_records_both_original_changes(self):
        base = {"a": 1}
        result = merge3(base, {"a": 2}, {"a": 3})
        assert result.merged == {"a": 2}
        assert kinds(result) == ["value"]
        conflict = result.conflicts[0]
        assert conflict.path == ("a",)
        assert conflict.base == 1
        assert conflict.ours == 2
        assert conflict.theirs == 3
        assert conflict.ours_change == {"kind": "value", "value": 2}
        assert conflict.theirs_change == {"kind": "value", "value": 3}
        assert conflict.json_pointer == "/a"

    def test_prefer_theirs_keeps_theirs_at_conflict(self):
        result = merge3({"a": 1}, {"a": 2}, {"a": 3}, prefer="theirs")
        assert result.merged == {"a": 3}
        assert result.conflicts[0].ours == 2

    def test_delete_modify_conflict(self):
        base = {"a": {"x": 1}, "b": 2}
        ours = {"b": 2}
        theirs = {"a": {"x": 2}, "b": 2}
        result = merge3(base, ours, theirs)
        assert kinds(result) == ["delete-modify"]
        assert result.conflicts[0].ours is MISSING
        # A one-sided delete must not silently discard the other side's edit.
        assert result.merged == {"a": {"x": 2}, "b": 2}

    def test_array_edits_merge_independently(self):
        base = [{"id": 1, "v": 1}, {"id": 2, "v": 1}, {"id": 3, "v": 1}]
        ours = [{"id": 1, "v": 2}, {"id": 2, "v": 1}, {"id": 3, "v": 1}]
        theirs = [{"id": 1, "v": 2}, {"id": 2, "v": 1}, {"id": 3, "v": 9}]
        result = merge3(base, ours, theirs)
        assert result.merged == [
            {"id": 1, "v": 2},
            {"id": 2, "v": 1},
            {"id": 3, "v": 9},
        ]
        assert result.clean

    def test_array_insertions_preserved(self):
        base = [{"id": 1}]
        ours = [{"id": 0}, {"id": 1}]
        theirs = [{"id": 1}, {"id": 2}]
        result = merge3(base, ours, theirs)
        assert result.merged == [{"id": 0}, {"id": 1}, {"id": 2}]
        assert result.clean


class TestSharedTargetCorrection:
    def test_target_correction_propagates_to_connected_references(self):
        base = {
            "target": {"limit": 1},
            "a": {"$ref": "#/target"},
            "b": {"$ref": "#/target"},
        }
        ours = {
            "target": {"limit": 2},
            "a": {"$ref": "#/target"},
            "b": {"$ref": "#/target"},
        }
        theirs = {
            "target": {"limit": 1, "mode": "strict"},
            "a": {"$ref": "#/target"},
            "b": {"$ref": "#/target"},
        }
        result = merge3(base, ours, theirs, document="config.json")
        assert result.clean
        assert result.merged["target"] == {"limit": 2, "mode": "strict"}
        assert result.merged["a"] == {"$ref": "#/target"}
        assert result.merged["b"] == {"$ref": "#/target"}
        update = result.analysis["shared_target_updates"][0]
        assert update["path"] == ["target"]
        assert update["reference_count"] == 2
        assert {item["path"][0] for item in update["affected_references"]} == {
            "a",
            "b",
        }

    def test_direct_edit_at_reference_site_does_not_rewrite_target(self):
        base = {"target": {"v": 1}, "r": {"$ref": "#/target"}}
        ours = {"target": {"v": 2}, "r": {"$ref": "#/target"}}
        theirs = {"target": {"v": 1}, "r": {"v": 1}}
        result = merge3(base, ours, theirs)
        assert result.clean
        assert result.merged == {"target": {"v": 2}, "r": {"v": 1}}
        update = result.analysis["shared_target_updates"][0]
        assert update["affected_references"] == []
        assert update["disconnected_references"] == [
            {
                "document": "",
                "path": ["r"],
                "json_pointer": "/r",
                "reference": "#/target",
                "state": "local-copy",
            }
        ]
        copy = result.analysis["local_copies"][0]
        assert copy["ours"] is None
        assert copy["theirs"]["matches_target"] is True

    def test_independent_equal_value_does_not_join_target_edits(self):
        base = {"target": {"v": 1}, "copy": {"v": 1}}
        ours = {"target": {"v": 2}, "copy": {"v": 1}}
        theirs = {"target": {"v": 1}, "copy": {"v": 1}}
        result = merge3(base, ours, theirs)
        assert result.merged == {"target": {"v": 2}, "copy": {"v": 1}}
        assert result.clean
        assert result.analysis["shared_target_updates"] == []

    def test_equal_values_on_both_sides_are_not_merged_into_one_object(self):
        base = {"a": {"v": 1}, "b": {"v": 1}}
        ours = {"a": {"v": 2}, "b": {"v": 1}}
        theirs = {"a": {"v": 1}, "b": {"v": 2}}
        result = merge3(base, ours, theirs)
        assert result.clean
        assert result.merged == {"a": {"v": 2}, "b": {"v": 2}}
        assert result.analysis["shared_target_updates"] == []

    def test_shared_target_conflict_is_reported_at_target(self):
        base = {"target": {"v": 1}, "r": {"$ref": "#/target"}}
        ours = {"target": {"v": 2}, "r": {"$ref": "#/target"}}
        theirs = {"target": {"v": 3}, "r": {"$ref": "#/target"}}
        result = merge3(base, ours, theirs)
        assert kinds(result) == ["value"]
        assert result.conflicts[0].path == ("target", "v")
        assert result.conflicts[0].ours == 2
        assert result.conflicts[0].theirs == 3
        assert result.merged["r"] == {"$ref": "#/target"}


class TestReferenceRedirection:
    def test_one_side_redirection_is_kept(self):
        base = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t1"}}
        ours = {"t1": {"v": 10}, "t2": {"v": 2}, "r": {"$ref": "#/t1"}}
        theirs = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t2"}}
        result = merge3(base, ours, theirs)
        assert result.clean
        assert result.merged["r"] == {"$ref": "#/t2"}
        redirect = result.analysis["reference_redirects"][0]
        assert redirect["from"] == "#/t1"
        assert redirect["theirs"]["to"] == "#/t2"
        update = result.analysis["shared_target_updates"][0]
        assert update["path"] == ["t1"]
        assert update["affected_references"] == []
        assert update["redirected_references"][0]["state"] == "redirected-away"
        assert update["disconnected_references"] == []

    def test_redirection_to_corrected_target_observes_correction(self):
        base = {"t1": {"v": 1}, "t2": {"v": 1}, "r": {"$ref": "#/t1"}}
        ours = {"t1": {"v": 1}, "t2": {"v": 5}, "r": {"$ref": "#/t1"}}
        theirs = {"t1": {"v": 1}, "t2": {"v": 1}, "r": {"$ref": "#/t2"}}
        result = merge3(base, ours, theirs)
        assert result.clean
        assert result.merged["t2"] == {"v": 5}
        assert result.merged["r"] == {"$ref": "#/t2"}
        update = result.analysis["shared_target_updates"][0]
        assert update["path"] == ["t2"]
        assert update["affected_references"] == []
        assert [
            item["path"] for item in update["newly_affected_references"]
        ] == [["r"]]

    def test_both_redirect_differently_conflicts(self):
        base = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t1"}}
        ours = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t2"}}
        theirs = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/missing"}}
        result = merge3(base, ours, theirs)
        assert kinds(result) == ["ref-redirect"]
        conflict = result.conflicts[0]
        assert conflict.ours_change == {
            "kind": "reference-redirect",
            "from": "#/t1",
            "to": "#/t2",
        }
        assert conflict.theirs_change == {
            "kind": "reference-redirect",
            "from": "#/t1",
            "to": "#/missing",
        }

    def test_redirect_versus_local_copy_conflicts(self):
        base = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t1"}}
        ours = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t2"}}
        theirs = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"v": 1}}
        result = merge3(base, ours, theirs)
        assert kinds(result) == ["ref-redirect-vs-local-copy"]
        conflict = result.conflicts[0]
        assert conflict.theirs_change["kind"] == "local-copy"
        assert conflict.theirs_change["matches_target"] is True
        assert conflict.ours_change["kind"] == "reference-redirect"

    def test_redirect_versus_literal_value_conflicts(self):
        base = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t1"}}
        ours = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"$ref": "#/t2"}}
        theirs = {"t1": {"v": 1}, "t2": {"v": 2}, "r": {"v": 99}}
        result = merge3(base, ours, theirs)
        assert kinds(result) == ["ref-vs-value"]

    def test_chained_reference_observes_terminal_target_correction(self):
        base = {"t": {"v": 1}, "s": {"$ref": "#/t"}, "r": {"$ref": "#/s"}}
        ours = {"t": {"v": 2}, "s": {"$ref": "#/t"}, "r": {"$ref": "#/s"}}
        theirs = {"t": {"v": 2}, "s": {"$ref": "#/t"}, "r": {"$ref": "#/s"}}
        result = merge3(base, ours, theirs)
        assert result.clean
        update = result.analysis["shared_target_updates"][0]
        assert update["path"] == ["t"]
        assert [item["path"][0] for item in update["affected_references"]] == [
            "s",
            "r",
        ]

    def test_equivalent_target_spellings_are_not_a_redirect(self):
        base = {"t": {"v": 1}, "r": {"$ref": "#/t"}}
        ours = {"t": {"v": 1}, "r": {"$ref": "#/t"}}
        theirs = {"t": {"v": 1}, "r": {"$ref": "d.json#/t"}}
        result = merge3(base, ours, theirs, document="d.json")
        assert result.clean
        assert result.merged["r"] == {"$ref": "d.json#/t"}
        assert result.analysis["reference_redirects"] == []

    def test_equivalent_spelling_with_other_side_correction_is_unchanged(self):
        base = {"t": {"v": 1}, "r": {"$ref": "#/t"}}
        ours = {"t": {"v": 2}, "r": {"$ref": "#/t"}}
        theirs = {"t": {"v": 1}, "r": {"$ref": "d.json#/t"}}
        result = merge3(base, ours, theirs, document="d.json")
        assert result.clean
        assert result.merged["r"] == {"$ref": "d.json#/t"}
        assert result.analysis["reference_redirects"] == []
        update = result.analysis["shared_target_updates"][0]
        assert update["affected_references"][0]["reference"] == "d.json#/t"


class TestCyclesAndTermination:
    def test_cyclic_documents_do_not_expand_forever(self):
        base = {
            "a": {"next": {"$ref": "#/b"}, "v": 1},
            "b": {"next": {"$ref": "#/a"}, "v": 1},
        }
        ours = {
            "a": {"next": {"$ref": "#/b"}, "v": 2},
            "b": {"next": {"$ref": "#/a"}, "v": 1},
        }
        theirs = {
            "a": {"next": {"$ref": "#/b"}, "v": 1},
            "b": {"next": {"$ref": "#/a"}, "v": 2},
        }
        result = merge3(base, ours, theirs)
        assert result.clean
        assert result.merged["a"]["v"] == 2
        assert result.merged["b"]["v"] == 2
        assert result.merged["a"]["next"] == {"$ref": "#/b"}

    def test_self_referential_reference_in_analysis_terminates(self):
        base = {"r": {"$ref": "#/r"}}
        result = merge3(base, base, base)
        assert result.clean
        assert result.analysis["shared_target_updates"] == []


class TestMergeFiles:
    def test_merge_files_keeps_reference_symbolic(self, tmp_path):
        base = write(tmp_path / "base.json", {"t": {"v": 1}, "r": {"$ref": "#/t"}})
        ours = write(tmp_path / "ours.json", {"t": {"v": 2}, "r": {"$ref": "#/t"}})
        theirs = write(
            tmp_path / "theirs.json", {"t": {"v": 1}, "r": {"$ref": "#/t"}}
        )
        result = merge_files(base, ours, theirs)
        assert result.merged == {"t": {"v": 2}, "r": {"$ref": "#/t"}}
        assert result.clean

    def test_three_way_merge_alias(self):
        assert three_way_merge({"a": 1}, {"a": 2}, {"a": 1}).merged == {"a": 2}


class TestDirectoryMerge:
    def make_tree(self, root, files):
        root.mkdir(parents=True, exist_ok=True)
        for name, value in files.items():
            write(root / name, value)
        return root

    def test_cross_file_shared_target_correction(self, tmp_path):
        base = self.make_tree(
            tmp_path / "base",
            {
                "base.json": {"shared": {"timeout": 1}},
                "app.json": {
                    "a": {"$ref": "base.json#/shared"},
                    "b": {"$ref": "base.json#/shared"},
                },
            },
        )
        ours = self.make_tree(
            tmp_path / "ours",
            {
                "base.json": {"shared": {"timeout": 2}},
                "app.json": {
                    "a": {"$ref": "base.json#/shared"},
                    "b": {"$ref": "base.json#/shared"},
                },
            },
        )
        theirs = self.make_tree(
            tmp_path / "theirs",
            {
                "base.json": {"shared": {"timeout": 1}},
                "app.json": {
                    "a": {"$ref": "base.json#/shared"},
                    "b": {"timeout": 1},
                },
            },
        )
        result = merge_directories(base, ours, theirs)
        assert result.clean
        assert result.merged_files["base.json"]["shared"]["timeout"] == 2
        assert result.merged_files["app.json"]["a"] == {
            "$ref": "base.json#/shared"
        }
        assert result.merged_files["app.json"]["b"] == {"timeout": 1}
        assert len(result.analysis["shared_target_updates"]) == 1
        update = result.analysis["shared_target_updates"][0]
        assert update["document"] == "base.json"
        assert update["path"] == ["shared"]
        assert update["reference_count"] == 2
        assert update["affected_references"] == [
            {
                "document": "app.json",
                "path": ["a"],
                "json_pointer": "/a",
                "reference": "base.json#/shared",
            }
        ]
        assert update["disconnected_references"][0]["state"] == "local-copy"

    def test_added_files(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"a.json": {"x": 1}})
        ours = self.make_tree(
            tmp_path / "ours", {"a.json": {"x": 1}, "b.json": {"y": 1}}
        )
        theirs = self.make_tree(
            tmp_path / "theirs", {"a.json": {"x": 1}, "c.json": {"z": 1}}
        )
        result = merge_directories(base, ours, theirs)
        assert result.clean
        assert result.added == {"b.json", "c.json"}
        assert result.merged_files["b.json"] == {"y": 1}
        assert result.merged_files["c.json"] == {"z": 1}

    def test_deleted_files(self, tmp_path):
        base = self.make_tree(
            tmp_path / "base", {"a.json": {"x": 1}, "b.json": {"y": 1}}
        )
        ours = self.make_tree(tmp_path / "ours", {"a.json": {"x": 1}})
        theirs = self.make_tree(tmp_path / "theirs", {"a.json": {"x": 1}})
        result = merge_directories(base, ours, theirs)
        assert result.deleted == {"b.json"}
        assert "b.json" not in result.merged_files

    def test_delete_modify_file_conflict(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"a.json": {"x": 1}})
        ours = self.make_tree(tmp_path / "ours", {"a.json": {"x": 2}})
        theirs = self.make_tree(tmp_path / "theirs", {})
        result = merge_directories(base, ours, theirs)
        assert [c.kind for c in result.conflicts] == ["file-delete-modify"]
        assert result.conflicts[0].ours == {"x": 2}
        assert result.conflicts[0].theirs is MISSING
        assert result.merged_files["a.json"] == {"x": 2}

    def test_write_preserves_untouched_bytes(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"a.json": {"x": 1}})
        ours = self.make_tree(tmp_path / "ours", {"a.json": {"x": 1}})
        theirs = self.make_tree(tmp_path / "theirs", {"a.json": {"x": 1}})
        result = merge_directories(base, ours, theirs)
        assert result.unchanged == {"a.json"}
        output = tmp_path / "out"
        result.write(output)
        assert json.loads((output / "a.json").read_text()) == {"x": 1}

    def test_file_order_does_not_affect_analysis(self, tmp_path):
        files = {
            "z.json": {"shared": {"v": 1}},
            "a.json": {"r": {"$ref": "z.json#/shared"}},
        }
        base = self.make_tree(tmp_path / "base", files)
        ours = self.make_tree(
            tmp_path / "ours",
            {
                "z.json": {"shared": {"v": 2}},
                "a.json": {"r": {"$ref": "z.json#/shared"}},
            },
        )
        theirs = self.make_tree(
            tmp_path / "theirs",
            {
                "z.json": {"shared": {"v": 2}},
                "a.json": {"r": {"$ref": "z.json#/shared"}},
            },
        )
        result = merge_directories(base, ours, theirs)
        assert result.clean
        update = result.analysis["shared_target_updates"][0]
        assert update["path"] == ["shared"]
        assert update["affected_references"][0]["document"] == "a.json"

    def test_directory_conflict_records_original_sides(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"c.json": {"v": 1}})
        ours = self.make_tree(tmp_path / "ours", {"c.json": {"v": 2}})
        theirs = self.make_tree(tmp_path / "theirs", {"c.json": {"v": 3}})
        result = merge_directories(base, ours, theirs)
        assert kinds(result) == ["value"]
        conflict = result.conflicts[0]
        assert conflict.document == "c.json"
        assert conflict.ours == 2
        assert conflict.theirs == 3

    def test_result_report_is_json_serializable(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"c.json": {"v": 1}})
        ours = self.make_tree(tmp_path / "ours", {"c.json": {"v": 2}})
        theirs = self.make_tree(tmp_path / "theirs", {"c.json": {"v": 3}})
        result = merge_directories(base, ours, theirs)
        payload = json.loads(json.dumps(result.to_dict()))
        assert payload["clean"] is False
        assert payload["conflicts"][0]["ours"] == 2
        assert payload["conflicts"][0]["theirs"] == 3


class TestAnalysis:
    def test_analyse_references_standalone(self):
        base = {"t": {"v": 1}, "r": {"$ref": "#/t"}}
        ours = {"t": {"v": 2}, "r": {"$ref": "#/t"}}
        theirs = {"t": {"v": 2}, "r": {"$ref": "#/t"}}
        analysis = analyse_references(
            {"d.json": base}, {"d.json": ours}, {"d.json": theirs}, {"d.json": ours}
        )
        assert analysis["shared_target_updates"][0]["reference_count"] == 1

    def test_merge_result_raise_for_conflicts(self):
        result = MergeResult({"a": 1}, [MergeConflict(("a",), "value")])
        with pytest.raises(MergeError) as error:
            result.raise_for_conflicts()
        assert len(error.value.conflicts) == 1

    def test_directory_result_raise_for_conflicts(self, tmp_path):
        base = self.make_tree(tmp_path / "base", {"c.json": {"v": 1}})
        ours = self.make_tree(tmp_path / "ours", {"c.json": {"v": 2}})
        theirs = self.make_tree(tmp_path / "theirs", {"c.json": {"v": 3}})
        result = merge_directories(base, ours, theirs)
        with pytest.raises(MergeError):
            result.raise_for_conflicts()

    def make_tree(self, root, files):
        root.mkdir(parents=True, exist_ok=True)
        for name, value in files.items():
            write(root / name, value)
        return root


class TestCLI:
    def run(self, argv):
        from jsonref.merge import main

        return main(argv)

    def test_file_cli_writes_output(self, tmp_path, capsys):
        base = write(tmp_path / "base.json", {"t": {"v": 1}, "r": {"$ref": "#/t"}})
        ours = write(tmp_path / "ours.json", {"t": {"v": 2}, "r": {"$ref": "#/t"}})
        theirs = write(tmp_path / "theirs.json", {"t": {"v": 1}, "r": {"$ref": "#/t"}})
        output = tmp_path / "merged.json"
        report = tmp_path / "report.json"
        code = self.run(
            [
                str(base),
                str(ours),
                str(theirs),
                "-o",
                str(output),
                "--report",
                str(report),
            ]
        )
        assert code == 0
        assert json.loads(output.read_text()) == {
            "t": {"v": 2},
            "r": {"$ref": "#/t"},
        }
        payload = json.loads(report.read_text())
        assert payload["clean"] is True

    def test_directory_cli_conflict_exit_code(self, tmp_path):
        for side, value in (("base", 1), ("ours", 2), ("theirs", 3)):
            write(tmp_path / side / "c.json", {"v": value})
        output = tmp_path / "out"
        code = self.run(
            [
                str(tmp_path / "base"),
                str(tmp_path / "ours"),
                str(tmp_path / "theirs"),
                "-o",
                str(output),
            ]
        )
        assert code == 1
        assert json.loads((output / "c.json").read_text()) == {"v": 2}

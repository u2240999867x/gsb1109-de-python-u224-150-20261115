import os

import pytest

import Evtx.Anonymize as anonymize
import Evtx.Evtx as evtx

NS = "http://schemas.microsoft.com/win/2004/08/events/event"


def one(iterable):
    for value in iterable:
        return value
    raise AssertionError("expected at least one value")


def event_xml(security="S-1-5-18", names=("alice", "bob"), computer="alice"):
    data = "".join('<Data Name="TargetUserName">%s</Data>' % name for name in names)
    return (
        '<Event xmlns="%s"><System><Security UserID="%s"></Security>'
        "<Computer>%s</Computer></System><EventData>%s</EventData></Event>" % (NS, security, computer, data)
    )


def test_anonymizes_only_selected_paths_and_keeps_other_copies():
    policy = {
        "paths": [
            "/Event/System/Security/@UserID",
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
        ]
    }
    xml = event_xml(names=("alice",), computer="alice")
    result = anonymize.anonymize_xml(xml, policy)

    assert 'UserID="' in result and "S-1-5-18" not in result
    assert "<Computer>alice</Computer>" in result
    assert ">alice<" not in result.split("<EventData>", 1)[1]


def test_identical_value_in_unselected_field_is_untouched():
    policy = {"paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"]}
    xml = event_xml(names=("alice",), computer="alice")
    result = anonymize.anonymize_xml(xml, policy)

    assert "<Computer>alice</Computer>" in result
    assert '<Data Name="TargetUserName">USER-' in result


def test_same_identifier_maps_consistently_across_events_and_fields():
    policy = {
        "paths": [
            "/Event/System/Security/@UserID",
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
            "/Event/EventData/Data[@Name='SubjectUserName']/text()",
        ]
    }
    anonymizer = anonymize.Anonymizer(policy)
    first = anonymizer.anonymize_xml(event_xml(security="S-1-5-18", names=("alice",)))
    second = anonymizer.anonymize_xml(event_xml(security="S-1-5-18", names=("alice",)))

    user_id = anonymizer.replacements["S-1-5-18"]
    alice = anonymizer.replacements["alice"]
    assert 'UserID="%s"' % user_id in first
    assert 'UserID="%s"' % user_id in second
    assert ">%s<" % alice in first
    assert ">%s<" % alice in second
    assert user_id != "S-1-5-18" and alice != "alice"


def test_same_value_in_selected_fields_gets_same_pseudonym():
    policy = {
        "paths": [
            "/Event/EventData/Data[@Name='SubjectUserName']/text()",
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
        ]
    }
    anonymizer = anonymize.Anonymizer(policy)
    result = anonymizer.anonymize_xml(
        event_xml(names=("alice",)).replace("</EventData>", '<Data Name="SubjectUserName">alice</Data></EventData>')
    )
    values = [part.split(">", 1)[1].split("<", 1)[0] for part in result.split('<Data Name="')[1:]]
    assert values[0] == values[-1]


def test_relations_force_related_fields_to_share_one_mapping():
    policy = {
        "paths": ["/Event/System/Security/@UserID"],
        "relations": [
            {
                "paths": [
                    "/Event/System/Security/@UserID",
                    "/Event/EventData/Data[@Name='TargetUserName']/text()",
                ],
                "mapping": {"S-1-5-18": "USER-RELATED", "SYSTEM": "USER-RELATED"},
            }
        ],
    }
    xml = event_xml(security="S-1-5-18", names=("SYSTEM",))
    result = anonymize.Anonymizer(policy).anonymize_xml(xml)

    assert result.count("USER-RELATED") == 2


def test_conflicting_relation_mappings_are_rejected():
    policy = {
        "relations": [
            {
                "paths": ["/Event/System/Security/@UserID"],
                "mapping": {"S-1-5-18": "USER-A"},
            },
            {
                "paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"],
                "mapping": {"S-1-5-18": "USER-B"},
            },
        ]
    }
    with pytest.raises(anonymize.PolicyError):
        anonymize.AnonymizationPolicy(policy)


def test_duplicate_positions_are_addressed_by_occurrence_not_value():
    policy = {"paths": ["/Event/EventData/Data[@Name='TargetUserName'][2]/text()"]}
    xml = event_xml(names=("alice", "alice"))
    result = anonymize.Anonymizer(policy).anonymize_xml(xml)

    assert '<Data Name="TargetUserName">alice</Data>' in result
    assert '<Data Name="TargetUserName">USER-' in result


def test_last_and_position_predicates():
    xml = event_xml(names=("a", "b", "c"))
    last = anonymize.anonymize_xml(xml, {"paths": ["/Event/EventData/Data[@Name='TargetUserName'][last()]/text()"]})
    assert '<Data Name="TargetUserName">a</Data>' in last
    assert '<Data Name="TargetUserName">b</Data>' in last
    assert '<Data Name="TargetUserName">c</Data>' not in last

    positioned = anonymize.anonymize_xml(
        xml, {"paths": ["/Event/EventData/Data[@Name='TargetUserName'][position()=2]/text()"]}
    )
    assert '<Data Name="TargetUserName">a</Data>' in positioned
    assert '<Data Name="TargetUserName">b</Data>' not in positioned


def test_namespace_prefixes_and_uris_participate_in_paths():
    xml = (
        '<Event xmlns="%s" xmlns:other="urn:other">'
        '<System><Security UserID="S-1-5-18"></Security>'
        '<other:Security UserID="S-1-5-19"></other:Security></System></Event>' % NS
    )
    policy = {
        "namespaces": {"e": NS, "other": "urn:other"},
        "paths": ["/e:Event/e:System/e:Security/@UserID"],
    }
    result = anonymize.anonymize_xml(xml, policy)

    assert "S-1-5-18" not in result.split("urn:other", 1)[1].split(">", 1)[1]
    assert 'UserID="S-1-5-19"' in result
    assert "S-1-5-18" not in result

    clark = anonymize.anonymize_xml(xml, {"paths": ["/{%s}Event/{%s}System/{%s}Security/@UserID" % (NS, NS, NS)]})
    assert "S-1-5-18" not in clark and 'UserID="S-1-5-19"' in clark


def test_element_local_name_in_other_namespace_does_not_match():
    xml = (
        '<Event xmlns="%s"><System><Security UserID="S-1-5-18"></Security>'
        '<Security xmlns="urn:other" UserID="S-1-5-19"></Security></System></Event>' % NS
    )
    policy = {"paths": ["/Event/System/Security/@UserID"]}
    result = anonymize.anonymize_xml(xml, policy)

    assert "S-1-5-18" not in result
    assert 'UserID="S-1-5-19"' in result


def test_explicit_mapping_wins_and_is_stable():
    policy = {
        "paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"],
        "mapping": {"alice": "USER-0001"},
    }
    result = anonymize.anonymize_xml(event_xml(names=("alice",)), policy)
    assert '<Data Name="TargetUserName">USER-0001</Data>' in result


def test_structure_remains_parseable_and_unchanged():
    xml = event_xml()
    policy = {
        "paths": [
            "/Event/System/Security/@UserID",
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
        ]
    }
    original = anonymize.parse_xml(xml)
    result = anonymize.anonymize_xml(xml, policy)
    anonymized = anonymize.parse_xml(result)

    assert anonymize.assert_structure_preserved(original, anonymized)
    assert anonymize.xml_to_dict(anonymized)["System"]["Security"]["@UserID"] != "S-1-5-18"


def test_exported_events_to_json_round_trip():
    policy = {"paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"]}
    events = [anonymize.parse_xml(anonymize.anonymize_xml(event_xml(), policy))]
    payload = anonymize.xml_to_dict(events[0])
    assert payload["EventData"]["Data"][0]["#text"].startswith("USER-")
    assert payload["EventData"]["Data"][1]["#text"].startswith("USER-")


def test_record_level_export_real_evtx():
    data_path = os.path.join(os.path.dirname(__file__), "data", "security.evtx")
    policy = {
        "paths": [
            "/Event/System/Security/@UserID",
            "/Event/EventData/Data[@Name='SubjectUserName']/text()",
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
        ]
    }
    anonymizer = anonymize.Anonymizer(policy)
    rendered = []
    originals = []
    with evtx.Evtx(data_path) as log:
        for record in list(log.records())[:25]:
            originals.append(record.xml())
            rendered.append(anonymizer.anonymize_record(record))

    assert len(rendered) == 25
    for xml, original_xml in zip(rendered, originals):
        original = anonymize.parse_xml(original_xml)
        after = anonymize.parse_xml(xml)
        assert anonymize.assert_structure_preserved(original, after)

    joined = "\n".join(rendered)
    # Only the two selected account-name paths change; the same-looking SID
    # in an unselected TargetUserSid path must survive untouched.
    assert '<Data Name="SubjectUserName">USER-' in joined
    assert 'TargetUserSid">S-1-5-18' in joined
    assert "37L4247F27-25" in joined


def test_deterministic_across_anonymizers():
    policy = {"paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"]}
    first = anonymize.Anonymizer(policy).replacement("alice")
    second = anonymize.Anonymizer(policy).replacement("alice")
    assert first == second


def test_prefix_salt_and_digest_length_are_configurable():
    policy = {
        "paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"],
        "prefix": "P-",
        "salt": "s",
        "digest_length": 4,
    }
    value = anonymize.Anonymizer(policy).replacement("alice")
    assert value.startswith("P-") and len(value) == 6


def test_counter_scheme_is_order_independent_for_same_identifier():
    policy = {
        "paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"],
        "scheme": "counter",
    }
    first = anonymize.Anonymizer(policy)
    assert first.replacement("alice") == first.replacement("alice")
    assert first.replacement("alice") != first.replacement("bob")


def test_skip_values_are_left_alone():
    policy = {"paths": ["/Event/EventData/Data[@Name='TargetUserName']/text()"]}
    result = anonymize.anonymize_xml(event_xml(names=("-",)), policy)
    assert '<Data Name="TargetUserName">-</Data>' in result


def test_descendant_axis_reaches_variable_depth_fields():
    policy = {"paths": ["/Event//Data[@Name='TargetUserName']/text()"]}
    xml = (
        '<Event xmlns="%s"><System><Data Name="TargetUserName">alice</Data></System>'
        '<EventData><Data Name="TargetUserName">bob</Data></EventData></Event>' % NS
    )
    result = anonymize.anonymize_xml(xml, policy)
    assert ">alice<" not in result and ">bob<" not in result


def test_attribute_wildcard_targets_every_attribute_of_selected_element():
    policy = {"paths": ["/Event/System/Security/@*"]}
    xml = '<Event xmlns="%s"><System><Security UserID="S-1-5-18" Other="keep-me"></Security></System></Event>' % NS
    result = anonymize.anonymize_xml(xml, policy)
    assert "S-1-5-18" not in result
    assert "keep-me" not in result


def test_overlapping_paths_do_not_replace_twice():
    policy = {
        "paths": [
            "/Event/EventData/Data[@Name='TargetUserName']/text()",
            "/Event/EventData/Data[@Name='TargetUserName']",
        ]
    }
    anonymizer = anonymize.Anonymizer(policy)
    result = anonymizer.anonymize_xml(event_xml(names=("alice",)))
    expected = anonymizer.replacements["alice"]

    assert '<Data Name="TargetUserName">%s</Data>' % expected in result


def test_wildcard_element_names_still_respect_namespaces_for_leaf_text():
    policy = {"paths": ["/Event//*[@Name='TargetUserName']/text()"]}
    xml = event_xml(names=("alice",))
    result = anonymize.anonymize_xml(xml, policy)
    assert '<Data Name="TargetUserName">USER-' in result
    assert "<Computer>alice</Computer>" in result


def test_wildcard_matching_any_namespace_is_explicit():
    policy = {"paths": ["//x:Secret/text()"], "namespaces": {"x": "urn:x"}}
    xml = (
        '<Event xmlns="%s" xmlns:x="urn:x"><x:Secret>top</x:Secret>'
        "<EventData><Data>value</Data></EventData></Event>" % NS
    )
    result = anonymize.anonymize_xml(xml, policy)
    assert "top" not in result
    assert ">value<" in result


def test_element_with_children_is_never_text_replaced():
    policy = {"paths": ["/Event/EventData"]}
    with pytest.raises(anonymize.StructureError):
        anonymize.anonymize_xml(event_xml(), policy)


def test_invalid_paths_are_rejected():
    for path in ("", "/", "/Event/System/Security/@", "/Event[0]", "/Event[", "Event//"):
        with pytest.raises(anonymize.PolicyError):
            anonymize.AnonymizationPolicy({"paths": [path]})


def test_policy_list_and_keyword_construction():
    path = "/Event/EventData/Data[@Name='TargetUserName']/text()"
    assert anonymize.AnonymizationPolicy([path]).paths == [path]
    keyword = anonymize.AnonymizationPolicy([path], mapping={"alice": "USER-A"})
    assert keyword.mapping["alice"] == "USER-A"


def test_path_object_index_selects_duplicate_occurrence():
    policy = {
        "paths": [
            {
                "path": "/Event/EventData/Data[@Name='TargetUserName']",
                "index": 2,
            }
        ]
    }
    result = anonymize.anonymize_xml(event_xml(names=("a", "b", "c")), policy)
    assert '<Data Name="TargetUserName">a</Data>' in result
    assert '<Data Name="TargetUserName">b</Data>' not in result
    assert '<Data Name="TargetUserName">c</Data>' in result


def test_path_object_namespace_prefixes_unqualified_segments():
    policy = {
        "namespaces": {"e": NS},
        "paths": [
            {
                "path": "Event/EventData/Data[@Name='TargetUserName']/text()",
                "namespace": "e",
            }
        ],
    }
    result = anonymize.anonymize_xml(event_xml(names=("alice",)), policy)
    assert "<Computer>alice</Computer>" in result
    assert '<Data Name="TargetUserName">USER-' in result


def test_path_components_list_form():
    policy = {"paths": [["Event", "EventData", "Data[@Name='TargetUserName']", "text()"]]}
    result = anonymize.anonymize_xml(event_xml(names=("alice",)), policy)
    assert "<Computer>alice</Computer>" in result
    assert '<Data Name="TargetUserName">alice</Data>' not in result

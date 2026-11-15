"""Tests for the multi-table reconciliation module."""

from __future__ import annotations

import pytest

from odfdo.reconcile import (
    ACTIVE,
    CONFLICT,
    DELETED,
    Reconciler,
    reconcile_tables,
)

KEY = ("org", "ref")


def _reconcile(tables, **kwargs):
    options = {
        "key_columns": KEY,
        "deleted_column": "deleted",
        "override_column": "override",
    }
    options.update(kwargs)
    return reconcile_tables(tables, **options)


def test_duplicate_records_merge_into_one_fact_with_two_sources():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice", "city": "X"},
        ],
        "billing": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice", "city": "X"},
        ],
    }
    result = _reconcile(tables)
    assert len(result.facts) == 1
    fact = result.facts[0]
    assert fact.key == ("A", "1")
    assert fact.status == ACTIVE
    assert fact.data == {"name": "Alice", "city": "X"}
    assert fact.source_tables == ("crm", "billing")
    assert len(fact.sources) == 2
    assert {source.table for source in fact.sources} == {"crm", "billing"}
    assert len(result.sources) == 2


def test_sources_table_keeps_every_delivered_record_in_order():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
            {"org": "A", "ref": "2", "version": 1, "name": "Bob"},
        ],
        "billing": [
            {"org": "A", "ref": "1", "version": 2, "name": "Alison"},
        ],
    }
    result = _reconcile(tables)
    assert [source.sequence for source in result.sources] == [0, 1, 2]
    assert [(source.table, source.version) for source in result.sources] == [
        ("crm", 1),
        ("crm", 1),
        ("billing", 2),
    ]
    rows = result.to_source_rows()
    assert rows[0]["table"] == "crm"
    assert rows[0]["org"] == "A"
    assert rows[0]["ref"] == "1"
    assert rows[0]["name"] == "Alice"
    assert rows[0]["tombstone"] is False
    assert rows[0]["override"] is False


def test_higher_version_wins_while_older_delivery_stays_in_sources():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
            {"org": "A", "ref": "1", "version": 3, "name": "Alison"},
            {"org": "A", "ref": "1", "version": 2, "name": "Alicia"},
        ],
    }
    result = _reconcile(tables)
    fact = result.fact_for(("A", "1"))
    assert fact.status == ACTIVE
    assert fact.version == 3
    assert fact.data == {"name": "Alison"}
    assert [source.version for source in fact.sources] == [1, 3, 2]


def test_same_version_identical_payload_is_not_a_conflict():
    tables = {
        "crm": [{"org": "A", "ref": "1", "version": 2, "name": "Alice"}],
        "billing": [{"org": "A", "ref": "1", "version": 2, "name": "Alice"}],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == ACTIVE
    assert fact.conflicts == ()


def test_same_version_different_payload_is_a_preserved_conflict():
    tables = {
        "crm": [{"org": "A", "ref": "1", "version": 2, "name": "Alice"}],
        "billing": [{"org": "A", "ref": "1", "version": 2, "name": "Alison"}],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == CONFLICT
    assert fact.version == 2
    assert len(fact.conflicts) == 2
    payloads = {conflict.data["name"] for conflict in fact.conflicts}
    assert payloads == {"Alice", "Alison"}
    assert result.conflict_keys == (("A", "1"),)
    assert result.conflicting_facts == (fact,)


def test_override_declaration_resolves_same_version_conflict():
    tables = {
        "crm": [{"org": "A", "ref": "1", "version": 2, "name": "Alice"}],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "name": "Alison",
                "override": True,
            }
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == ACTIVE
    assert fact.data == {"name": "Alison"}
    assert fact.conflicts == ()


def test_override_flag_accepts_string_tokens():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 2, "name": "Alice", "deleted": "yes"},
        ],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "name": "Alison",
                "deleted": "no",
                "override": "true",
            }
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == ACTIVE
    assert fact.data == {"name": "Alison"}
    source = next(record for record in result.sources if record.table == "crm")
    assert source.tombstone is True


def test_explicit_tombstone_deletes_the_fact():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "name": None,
                "deleted": True,
            },
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == DELETED
    assert fact.deleted is True
    assert fact.version == 2
    assert fact.data == {}
    assert result.deleted_keys == (("A", "1"),)
    assert result.deleted_facts == (fact,)
    rows = result.to_fact_rows()
    assert rows[0]["status"] == "deleted"
    assert rows[0]["name"] is None


def test_newer_delivery_after_tombstone_revives_the_fact():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice", "deleted": False},
            {"org": "A", "ref": "1", "version": 2, "deleted": True},
            {"org": "A", "ref": "1", "version": 3, "name": "Alice again"},
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == ACTIVE
    assert fact.version == 3
    assert fact.data == {"name": "Alice again"}
    assert len(fact.sources) == 3


def test_tombstone_and_live_row_at_same_version_conflict():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 2, "deleted": True},
        ],
        "billing": [
            {"org": "A", "ref": "1", "version": 2, "name": "Alice"},
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == CONFLICT
    assert len(fact.conflicts) == 2
    assert any(conflict.tombstone for conflict in fact.conflicts)


def test_undelivered_table_does_not_delete_its_records():
    first = _reconcile(
        {
            "crm": [
                {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
                {"org": "A", "ref": "2", "version": 1, "name": "Bob"},
            ],
            "billing": [
                {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
            ],
        }
    )
    assert first.delivered_tables == ("crm", "billing")

    later = _reconcile(
        {
            "crm": [
                {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
                {"org": "A", "ref": "2", "version": 1, "name": "Bob"},
            ],
        }
    )
    assert later.delivered_tables == ("crm",)
    assert {fact.key for fact in later.facts} == {("A", "1"), ("A", "2")}
    assert later.deleted_keys == ()
    missing = later.fact_for(("A", "1"))
    assert missing.status == ACTIVE
    assert missing.source_tables == ("crm",)


def test_reordered_key_columns_in_row_do_not_change_identity():
    tables = {
        "crm": [
            {"ref": "1", "org": "A", "version": 1, "name": "Alice"},
        ],
        "billing": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice"},
        ],
    }
    result = _reconcile(tables)
    assert len(result.facts) == 1
    assert result.facts[0].key == ("A", "1")


def test_same_payload_with_different_business_keys_stays_two_facts():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice", "city": "X"},
            {"org": "B", "ref": "1", "version": 1, "name": "Alice", "city": "X"},
        ],
    }
    result = _reconcile(tables)
    assert len(result.facts) == 2
    assert {fact.key for fact in result.facts} == {("A", "1"), ("B", "1")}
    assert all(fact.status == ACTIVE for fact in result.facts)


def test_facts_and_sources_rows_include_all_configured_columns():
    tables = {
        "crm": [
            {
                "org": "A",
                "ref": "1",
                "version": 1,
                "name": "Alice",
                "city": "X",
                "extra": "ignored",
            },
        ],
    }
    result = _reconcile(
        tables,
        data_columns=("name", "city", "missing"),
    )
    fact_row = result.to_fact_rows()[0]
    assert fact_row["org"] == "A"
    assert fact_row["ref"] == "1"
    assert fact_row["version"] == 1
    assert fact_row["status"] == "active"
    assert fact_row["name"] == "Alice"
    assert fact_row["city"] == "X"
    assert fact_row["source_tables"] == ("crm",)
    source_row = result.to_source_rows()[0]
    assert source_row["name"] == "Alice"
    assert "extra" not in source_row
    assert source_row["missing"] is None
    assert result.data_columns == ("name", "city", "missing")


def test_auto_discovered_data_columns_keep_first_seen_order():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 1, "name": "Alice", "city": "X"},
        ],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "city": "Y",
                "name": "Alison",
                "phone": "00",
            },
        ],
    }
    result = _reconcile(tables)
    assert result.data_columns == ("name", "city", "phone")
    assert result.facts[0].data == {"city": "Y", "name": "Alison", "phone": "00"}


def test_reconciler_accepts_iterable_of_table_pairs():
    reconciler = Reconciler(KEY, deleted_column="deleted", override_column="override")
    reconciler.register_table(
        "crm", [{"org": "A", "ref": "1", "version": 1, "name": "Alice"}]
    )
    reconciler.register_table(
        "billing", [{"org": "A", "ref": "1", "version": 1, "name": "Alice"}]
    )
    result = reconciler.reconcile()
    assert len(result.facts) == 1
    assert result.facts[0].source_tables == ("crm", "billing")


def test_missing_key_column_raises():
    reconciler = Reconciler(KEY)
    with pytest.raises(ValueError, match="missing key column"):
        reconciler.register_table("crm", [{"org": "A", "version": 1, "name": "Alice"}])


def test_duplicate_table_delivery_raises():
    reconciler = Reconciler(KEY)
    rows = [{"org": "A", "ref": "1", "version": 1, "name": "Alice"}]
    reconciler.register_table("crm", rows)
    with pytest.raises(ValueError, match="already delivered"):
        reconciler.register_table("crm", rows)


def test_invalid_boolean_token_raises():
    reconciler = Reconciler(KEY, deleted_column="deleted")
    with pytest.raises(ValueError, match="cannot interpret"):
        reconciler.register_table(
            "crm",
            [{"org": "A", "ref": "1", "version": 1, "deleted": "maybe"}],
        )


def test_reconciler_requires_key_columns():
    with pytest.raises(ValueError, match="at least one key column"):
        Reconciler(())


def test_fact_for_unknown_key_returns_none():
    result = _reconcile(
        {"crm": [{"org": "A", "ref": "1", "version": 1, "name": "Alice"}]}
    )
    assert result.fact_for(("Z", "9")) is None


def test_nested_payloads_are_compared_by_value():
    tables = {
        "crm": [
            {
                "org": "A",
                "ref": "1",
                "version": 1,
                "tags": ["x", "y"],
                "meta": {"k": 1},
            }
        ],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 1,
                "meta": {"k": 1},
                "tags": ["x", "y"],
            }
        ],
    }
    result = _reconcile(tables)
    assert result.facts[0].status == ACTIVE


def test_versions_of_unrelated_types_still_select_a_maximum():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": "v1", "name": "Alice"},
            {"org": "A", "ref": "1", "version": "v2", "name": "Alison"},
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.version == "v2"
    assert fact.data == {"name": "Alison"}


def test_delivery_order_breaks_ties_without_override():
    tables = {
        "crm": [{"org": "A", "ref": "1", "version": 2, "name": "Alice"}],
        "billing": [{"org": "A", "ref": "1", "version": 2, "name": "Alison"}],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == CONFLICT
    assert fact.data == {"name": "Alison"}


def test_two_overrides_disagreeing_remain_a_conflict():
    tables = {
        "crm": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "name": "Alice",
                "override": True,
            }
        ],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "name": "Alison",
                "override": True,
            }
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == CONFLICT
    assert len(fact.conflicts) == 2
    assert all(conflict.override for conflict in fact.conflicts)


def test_override_tombstone_wins_over_same_version_live_row():
    tables = {
        "crm": [
            {"org": "A", "ref": "1", "version": 2, "name": "Alice"},
        ],
        "billing": [
            {
                "org": "A",
                "ref": "1",
                "version": 2,
                "deleted": True,
                "override": True,
            }
        ],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == DELETED
    assert fact.version == 2
    assert fact.data == {}


def test_older_version_conflict_is_superseded_by_newer_delivery():
    tables = {
        "crm": [{"org": "A", "ref": "1", "version": 1, "name": "Alice"}],
        "billing": [{"org": "A", "ref": "1", "version": 1, "name": "Alison"}],
        "erp": [{"org": "A", "ref": "1", "version": 2, "name": "Aline"}],
    }
    result = _reconcile(tables)
    fact = result.facts[0]
    assert fact.status == ACTIVE
    assert fact.version == 2
    assert fact.data == {"name": "Aline"}
    assert fact.source_tables == ("crm", "billing", "erp")

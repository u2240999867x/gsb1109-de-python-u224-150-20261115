"""
Cross-section interface asset inventory.

Interfaces described in different sections of a pcap-ng file may actually
refer to the same device. Names cannot be trusted for this correlation:
some interfaces only carry a name, and a device keeps its old name after
its card (and thus its MAC address) has been swapped.

This module aggregates all :py:class:`pcapng.blocks.InterfaceDescription`
blocks found across the sections of a capture into an asset inventory:

* Correlation is based on explicit, stable device identifiers
  (``if_MACaddr`` and ``if_EUIaddr``) and on a user-supplied manual
  mapping table associating interface names that are known to belong
  to the same device.
* Interface names (``if_name``) are only kept as aliases of the resulting
  assets; they never trigger a merge on their own.
* A merge that would bring together conflicting stable identifiers (e.g.
  two different MAC addresses) is blocked, and the original interface
  description blocks that caused the conflict are reported.
"""

from pcapng.blocks import InterfaceDescription, SectionHeader
from pcapng.scanner import FileScanner

#: Option names used as stable device identifiers, mapped to the
#: attribute name used on records/groups.
STABLE_IDENTIFIERS = {
    "if_MACaddr": "mac",
    "if_EUIaddr": "eui",
}


class InterfaceRecord(object):
    """
    A single interface description, tied to the section it was found in.

    Wraps the original :py:class:`pcapng.blocks.InterfaceDescription`
    block, available as :attr:`block`.
    """

    __slots__ = ["section_index", "interface_id", "block"]

    def __init__(self, section_index, interface_id, block):
        self.section_index = section_index
        self.interface_id = interface_id
        self.block = block

    @property
    def name(self):
        """The ``if_name`` option, or None. Only an alias, never an identity."""
        return self.block.options.get("if_name")

    @property
    def description(self):
        """The ``if_description`` option, or None."""
        return self.block.options.get("if_description")

    @property
    def mac(self):
        """The ``if_MACaddr`` option (stable identifier), or None."""
        return self.block.options.get("if_MACaddr")

    @property
    def eui(self):
        """The ``if_EUIaddr`` option (stable identifier), or None."""
        return self.block.options.get("if_EUIaddr")

    def __repr__(self):
        return "<InterfaceRecord section={0} interface_id={1} name={2!r}>".format(
            self.section_index, self.interface_id, self.name
        )


class AssetConflict(object):
    """
    A blocked merge between two groups of interface records.

    Created when correlating records would require merging two groups
    that carry conflicting values for a stable identifier.

    :ivar identifier: name of the conflicting option
        (``"if_MACaddr"`` or ``"if_EUIaddr"``)
    :ivar values: the two conflicting ``(value_a, value_b)`` values
    :ivar reason: what triggered the attempted merge (``"shared_mac"``,
        ``"shared_eui"`` or ``"manual_mapping"``)
    :ivar reason_detail: the shared identifier value or manual mapping
        label that triggered the attempted merge
    :ivar records: the :py:class:`InterfaceRecord` objects from both
        conflicting groups
    """

    __slots__ = ["identifier", "values", "reason", "reason_detail", "records"]

    def __init__(self, identifier, values, reason, reason_detail, records):
        self.identifier = identifier
        self.values = tuple(values)
        self.reason = reason
        self.reason_detail = reason_detail
        self.records = tuple(records)

    @property
    def blocks(self):
        """The original InterfaceDescription blocks causing the conflict."""
        return [record.block for record in self.records]

    def __repr__(self):
        return (
            "<AssetConflict {0} {1[0]!r} != {1[1]!r} ({2} {3!r}, "
            "{4} record(s))>"
        ).format(
            self.identifier,
            self.values,
            self.reason,
            self.reason_detail,
            len(self.records),
        )


class Asset(object):
    """
    An inventoried device: a group of interface descriptions that were
    correlated across sections.
    """

    __slots__ = ["asset_id", "records"]

    def __init__(self, asset_id, records):
        self.asset_id = asset_id
        self.records = list(records)

    @property
    def mac(self):
        """The stable MAC address of this asset, or None."""
        for record in self.records:
            if record.mac is not None:
                return record.mac
        return None

    @property
    def eui(self):
        """The stable EUI address of this asset, or None."""
        for record in self.records:
            if record.eui is not None:
                return record.eui
        return None

    @property
    def aliases(self):
        """All interface names seen for this asset, sorted. Aliases only."""
        return sorted({r.name for r in self.records if r.name is not None})

    @property
    def descriptions(self):
        """All interface descriptions seen for this asset, sorted."""
        return sorted({r.description for r in self.records if r.description})

    @property
    def blocks(self):
        """The original InterfaceDescription blocks grouped in this asset."""
        return [record.block for record in self.records]

    def __repr__(self):
        return "<Asset #{0} aliases={1!r} mac={2!r} eui={3!r}>".format(
            self.asset_id, self.aliases, self.mac, self.eui
        )


class InterfaceInventory(object):
    """
    Result of correlating interface descriptions across sections.

    :ivar assets: list of :py:class:`Asset`
    :ivar conflicts: list of :py:class:`AssetConflict` (blocked merges)
    """

    __slots__ = ["assets", "conflicts"]

    def __init__(self, assets, conflicts):
        self.assets = assets
        self.conflicts = conflicts

    def __repr__(self):
        return "<InterfaceInventory {0} asset(s), {1} conflict(s)>".format(
            len(self.assets), len(self.conflicts)
        )


class _Group(object):
    """Mutable union-find group state."""

    __slots__ = ["mac", "eui", "records"]

    def __init__(self):
        self.mac = None
        self.eui = None
        self.records = []


def collect_interface_records(blocks):
    """
    Extract :py:class:`InterfaceRecord` objects from an iterable of blocks
    (e.g. a :py:class:`pcapng.scanner.FileScanner`), tracking the section
    each interface description belongs to.
    """
    records = []
    section_index = -1
    for block in blocks:
        if isinstance(block, SectionHeader):
            section_index += 1
        elif isinstance(block, InterfaceDescription):
            records.append(
                InterfaceRecord(section_index, block.interface_id, block)
            )
    return records


def build_inventory(blocks, name_map=None):
    """
    Correlate interface descriptions across sections into an inventory.

    :param blocks:
        an iterable of pcap-ng blocks (e.g. a
        :py:class:`pcapng.scanner.FileScanner` or a list of blocks).
        Interface descriptions are correlated across all the sections
        found in the stream.
    :param name_map:
        optional manual mapping table, as a dict mapping an interface
        name (alias) to an arbitrary hashable device label. Interfaces
        whose names map to the same label are correlated -- unless they
        carry conflicting stable identifiers, in which case the merge is
        blocked and reported as a conflict.
    :return: an :py:class:`InterfaceInventory`
    """
    records = collect_interface_records(blocks)
    parents = list(range(len(records)))
    groups = [_Group() for _ in records]
    for index, record in enumerate(records):
        groups[index].mac = record.mac
        groups[index].eui = record.eui
        groups[index].records.append(record)

    conflicts = []

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def try_merge(index_a, index_b, reason, reason_detail):
        root_a, root_b = find(index_a), find(index_b)
        if root_a == root_b:
            return
        group_a, group_b = groups[root_a], groups[root_b]
        for identifier, attr in STABLE_IDENTIFIERS.items():
            value_a = getattr(group_a, attr)
            value_b = getattr(group_b, attr)
            if value_a is not None and value_b is not None and value_a != value_b:
                conflicts.append(
                    AssetConflict(
                        identifier=identifier,
                        values=(value_a, value_b),
                        reason=reason,
                        reason_detail=reason_detail,
                        records=group_a.records + group_b.records,
                    )
                )
                return  # conflicting stable identifiers: block the merge
        parents[root_b] = root_a
        if group_a.mac is None:
            group_a.mac = group_b.mac
        if group_a.eui is None:
            group_a.eui = group_b.eui
        group_a.records.extend(group_b.records)

    # Correlate by explicit stable identifiers first.
    for identifier, attr in STABLE_IDENTIFIERS.items():
        seen = {}
        for index, record in enumerate(records):
            value = getattr(record, attr)
            if value is None:
                continue
            if value in seen:
                try_merge(seen[value], index, "shared_" + attr, value)
            else:
                seen[value] = index

    # Then correlate by the manual mapping table (names are aliases only;
    # they never merge anything on their own).
    if name_map:
        by_label = {}
        for index, record in enumerate(records):
            if record.name is not None and record.name in name_map:
                by_label.setdefault(name_map[record.name], []).append(index)
        for label, indices in by_label.items():
            for index in indices[1:]:
                try_merge(indices[0], index, "manual_mapping", label)

    assets = []
    for index in range(len(records)):
        if find(index) == index:
            assets.append(Asset(len(assets), groups[index].records))
    return InterfaceInventory(assets, conflicts)


def scan_inventory(stream, name_map=None):
    """
    Build an :py:class:`InterfaceInventory` from a pcap-ng stream.

    Shortcut for ``build_inventory(FileScanner(stream), name_map)``.
    """
    return build_inventory(FileScanner(stream), name_map=name_map)

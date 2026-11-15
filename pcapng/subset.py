"""
Tools for exporting a subset of the packets contained in a pcapng stream
into a new, self-contained pcapng stream.

The generated file only carries the sections, interface descriptions and
options that are actually needed to interpret the exported packets:

* a (new) section is only created when at least one of its packets is
  selected, and the section header options (eg. ``shb_hardware``,
  ``shb_os``, ``shb_userappl``) are copied over so the file stays
  self-explanatory;
* only the interfaces referenced by selected packets are described, with
  their options copied verbatim -- including ``if_tsresol``, so the
  original timestamp resolution is preserved;
* packet timestamps are copied as their raw high/low 32-bit halves, so
  no precision is lost in a round-trip through floating point values;
* blocks that are not selected (including their payloads) are never
  written to the output;
* packets that carry no timestamp of their own (eg.
  :py:class:`pcapng.blocks.SimplePacket`) are written as-is -- no
  capture time is ever invented for them.
"""

import pcapng.blocks as blocks
from pcapng.scanner import FileScanner
from pcapng.writer import FileWriter

__all__ = ["export_subset"]


def export_subset(instream, outstream, start=None, end=None, predicate=None):
    """
    Export a subset of the packets from a pcapng stream into a new stream.

    A packet is selected when:

    * its timestamp falls within the given ``start``/``end`` bounds
      (either bound may be omitted), or
    * ``predicate`` is provided and returns a true value for it.

    If no bounds and no predicate are given, all packets are exported.

    Packets that do not carry a timestamp (eg.
    :py:class:`pcapng.blocks.SimplePacket`) cannot be matched by time
    bounds; they are only exported when selected by ``predicate``. They
    are written out unchanged -- no timestamp is fabricated for them.

    :param instream:
        a file-like object to read the source capture from.

    :param outstream:
        a file-like object to write the new capture to.

    :param start:
        minimum (inclusive) packet timestamp, in seconds.

    :param end:
        maximum (inclusive) packet timestamp, in seconds.

    :param predicate:
        optional callable receiving each packet block; the packet is
        selected when it returns a true value.

    :returns:
        the number of packet blocks written to the output stream.
    """
    scanner = FileScanner(instream)
    writer = None
    exporters = {}
    exported = 0

    for block in scanner:
        if isinstance(block, blocks.SectionHeader):
            continue
        if not isinstance(block, blocks.BasePacketBlock):
            continue
        if not _packet_selected(block, start, end, predicate):
            continue
        exporter = exporters.get(id(block.section))
        if exporter is None:
            exporter = _SectionSubsetExporter(block.section)
            exporters[id(block.section)] = exporter
            if writer is None:
                writer = FileWriter(outstream, exporter.shb)
            else:
                writer.write_block(exporter.shb)
        exporter.export_packet(block, writer)
        exported += 1

    return exported


def _packet_selected(block, start, end, predicate):
    if predicate is not None and predicate(block):
        return True
    if start is None and end is None:
        return predicate is None
    timestamp = _packet_timestamp(block)
    if timestamp is None:
        # No timestamp to compare against the bounds, and we never
        # invent one: only an explicit predicate may select this packet.
        return False
    if start is not None and timestamp < start:
        return False
    if end is not None and timestamp > end:
        return False
    return True


def _packet_timestamp(block):
    if isinstance(block, blocks.BlockWithTimestampMixin):
        return block.timestamp
    return None


def _copy_options(source, target):
    """Copy option values verbatim (numeric codes and all values)."""
    for code, values in source.data.items():
        if code == 0:  # opt_endofopt is never written explicitly
            continue
        target.data[code] = list(values)


class _SectionSubsetExporter(object):
    """
    Builds the output counterpart of a source section: a new section
    header, plus only the interface descriptions that are actually
    referenced by the exported packets.
    """

    def __init__(self, source_shb):
        self.source_shb = source_shb
        self.shb = blocks.SectionHeader(
            endianness=source_shb.endianness,
            version_major=source_shb.version_major,
            version_minor=source_shb.version_minor,
        )
        _copy_options(source_shb.options, self.shb.options)
        # Maps source interface ids to the new InterfaceDescription blocks
        self.interfaces = {}

    def export_packet(self, block, writer):
        source_interface = block.interface
        if source_interface.interface_id not in self.interfaces:
            idb = self._export_interface(source_interface)
            writer.write_block(idb)
        writer.write_block(self._copy_packet(block))

    def _export_interface(self, source_interface):
        idb = self.shb.new_member(
            blocks.InterfaceDescription,
            link_type=source_interface.link_type,
            reserved=source_interface.reserved,
            snaplen=source_interface.snaplen,
        )
        # Copying the options verbatim also carries over ``if_tsresol``,
        # so the exported timestamps keep their original resolution.
        _copy_options(source_interface.options, idb.options)
        self.interfaces[source_interface.interface_id] = idb
        return idb

    def _copy_packet(self, block):
        if isinstance(block, blocks.ObsoletePacket):
            block = block.enhanced()
        if isinstance(block, blocks.EnhancedPacket):
            new_block = self.shb.new_member(
                blocks.EnhancedPacket,
                interface_id=self.interfaces[block.interface_id].interface_id,
                # Raw high/low halves: no float round-trip, no precision loss
                timestamp_high=block.timestamp_high,
                timestamp_low=block.timestamp_low,
                packet_len=block.packet_len,
                packet_data=block.packet_data,
            )
            _copy_options(block.options, new_block.options)
            return new_block
        if isinstance(block, blocks.SimplePacket):
            # Simple packets have no timestamp (nor interface id) of
            # their own; write them as-is without inventing one.
            return self.shb.new_member(
                blocks.SimplePacket,
                packet_len=block.packet_len,
                packet_data=block.packet_data,
            )
        raise TypeError("don't know how to export block {0!r}".format(block))

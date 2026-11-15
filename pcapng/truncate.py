"""
Truncate packet payloads in a pcap-ng stream to a maximum capture length,
producing a "sub-file" suitable for handing off to an analysis station,
while preserving file-level capture statistics.

The rewriting rules are:

* Packet data is shortened to at most ``snaplen`` bytes; block lengths and
  captured lengths are recomputed accordingly, but the original on-the-wire
  packet length (``packet_len``) is always preserved verbatim.
* Interface Description Blocks are kept for every interface, with their
  ``snaplen`` updated to reflect the new truncation limit (required for
  Simple Packet Blocks to be parsed back correctly).
* Interface Statistics Blocks are kept for every interface -- even
  interfaces that no longer have any packet in the sub-file, since they
  still carry capture coverage declarations (start/end times).

  The statistics they carry are split in two clearly separated groups:

  - the *original full-capture* counters (``isb_ifrecv``, ``isb_ifdrop``,
    ``isb_filteraccept``, ``isb_osdrop``, ``isb_usrdeliv``) are moved into
    an ``opt_comment`` option, so the original drop/receive counts are
    preserved but cannot be mistaken for results of the sub-file;
  - the *sub-file actual* statistics are reported through ``isb_usrdeliv``,
    set to the number of packets actually written for that interface.

* Packet hashes (``epb_hash`` / ``pack_hash``) are dropped, as they would
  no longer match the truncated packet data.
* Name Resolution Blocks and unknown blocks are passed through unchanged.
"""

from collections import Counter

import pcapng.blocks as blocks
from pcapng.scanner import FileScanner
from pcapng.structs import write_bytes_padded, write_int
from pcapng.writer import FileWriter

#: ISB options holding counters of the original full capture. They are
#: preserved as comments in the sub-file, never as standard options.
ORIGINAL_STATS_OPTIONS = (
    "isb_ifrecv",
    "isb_ifdrop",
    "isb_filteraccept",
    "isb_osdrop",
    "isb_usrdeliv",
)

COMMENT_ORIGINAL_STATS = (
    "Original full-capture statistics "
    "(whole capture, not this truncated sub-file): {stats}"
)

COMMENT_SUBFILE_STATS = (
    "Truncated sub-file: isb_usrdeliv reports the packets "
    "actually included for this interface"
)


def _copy_options(options, skip=()):
    """Copy an Options object into a plain dict, skipping given names"""
    copied = {}
    for name, values in options.iter_all_items():
        if name in skip:
            continue
        copied[name] = values if len(values) > 1 else values[0]
    return copied


def _write_unknown_block(stream, block, endianness):
    """Write an unknown block back to the stream, as raw data"""
    data = block.data
    padding = -len(data) % 4
    total_length = 12 + len(data) + padding
    write_int(block.block_type, stream, 32, endianness=endianness)
    write_int(total_length, stream, 32, endianness=endianness)
    write_bytes_padded(stream, data)
    write_int(total_length, stream, 32, endianness=endianness)


class _SectionTruncator(object):
    """Rewrites the blocks of one section into the output stream"""

    def __init__(self, outstream, shb, snaplen):
        self.outstream = outstream
        self.snaplen = snaplen
        self.delivered = Counter()

        new_shb = blocks.SectionHeader(
            endianness=shb.endianness,
            version_major=shb.version_major,
            version_minor=shb.version_minor,
            # The section length changes with truncation: leave unspecified
            section_length=-1,
            options=_copy_options(shb.options),
        )
        self.shb = new_shb
        self.writer = FileWriter(outstream, new_shb)

    def process(self, block):
        if isinstance(block, blocks.InterfaceDescription):
            self._process_interface(block)
        elif isinstance(block, blocks.EnhancedPacket):
            self._process_enhanced_packet(block)
        elif isinstance(block, blocks.SimplePacket):
            self._process_simple_packet(block)
        elif isinstance(block, blocks.ObsoletePacket):
            self._process_obsolete_packet(block)
        elif isinstance(block, blocks.InterfaceStatistics):
            self._process_interface_stats(block)
        elif isinstance(block, blocks.NameResolution):
            self._process_name_resolution(block)
        elif isinstance(block, blocks.UnknownBlock):
            _write_unknown_block(self.outstream, block, self.shb.endianness)
        # Anything else (shouldn't happen for known blocks) is dropped

    def _process_interface(self, block):
        if block.snaplen:
            snaplen = min(block.snaplen, self.snaplen)
        else:  # 0 means "no limit": declare the new truncation limit
            snaplen = self.snaplen
        new_idb = self.shb.new_member(
            blocks.InterfaceDescription,
            link_type=block.link_type,
            reserved=block.reserved,
            snaplen=snaplen,
            options=_copy_options(block.options),
        )
        self.writer.write_block(new_idb)

    def _process_enhanced_packet(self, block):
        new_epb = self.shb.new_member(
            blocks.EnhancedPacket,
            interface_id=block.interface_id,
            timestamp_high=block.timestamp_high,
            timestamp_low=block.timestamp_low,
            # Original on-the-wire length is preserved; only the
            # captured data (and hence captured_len / block length)
            # is shortened.
            packet_len=block.packet_len,
            packet_data=block.packet_data[: self.snaplen],
            # Packet hashes would no longer match the truncated data
            options=_copy_options(block.options, skip=("epb_hash",)),
        )
        self.writer.write_block(new_epb)
        self.delivered[block.interface_id] += 1

    def _process_simple_packet(self, block):
        new_spb = self.shb.new_member(
            blocks.SimplePacket,
            packet_len=block.packet_len,
            packet_data=block.packet_data[: self.snaplen],
        )
        self.writer.write_block(new_spb)
        self.delivered[block.interface_id] += 1

    def _process_obsolete_packet(self, block):
        options = _copy_options(block.options, skip=("pack_hash",))
        if "pack_flags" in options:
            options["epb_flags"] = options.pop("pack_flags")
        options["epb_dropcount"] = block.drops_count
        new_epb = self.shb.new_member(
            blocks.EnhancedPacket,
            interface_id=block.interface_id,
            timestamp_high=block.timestamp_high,
            timestamp_low=block.timestamp_low,
            packet_len=block.packet_len,
            packet_data=block.packet_data[: self.snaplen],
            options=options,
        )
        self.writer.write_block(new_epb)
        self.delivered[block.interface_id] += 1

    def _process_interface_stats(self, block):
        # Split the original full-capture counters away from the
        # statistics describing the sub-file actually being written.
        original = {}
        for name in ORIGINAL_STATS_OPTIONS:
            if name in block.options:
                original[name] = block.options[name]

        options = _copy_options(block.options, skip=ORIGINAL_STATS_OPTIONS)
        # Sub-file actual statistics: packets really included so far
        # for this interface (possibly zero -- the coverage declaration
        # carried by this block is preserved either way).
        options["isb_usrdeliv"] = self.delivered[block.interface_id]

        new_isb = self.shb.new_member(
            blocks.InterfaceStatistics,
            interface_id=block.interface_id,
            timestamp_high=block.timestamp_high,
            timestamp_low=block.timestamp_low,
            options=options,
        )
        if original:
            stats = ", ".join(
                "{0}={1}".format(name, original[name])
                for name in ORIGINAL_STATS_OPTIONS
                if name in original
            )
            new_isb.options.add(
                "opt_comment", COMMENT_ORIGINAL_STATS.format(stats=stats)
            )
        new_isb.options.add("opt_comment", COMMENT_SUBFILE_STATS)
        self.writer.write_block(new_isb)

    def _process_name_resolution(self, block):
        new_nrb = self.shb.new_member(
            blocks.NameResolution,
            records=block.records,
            options=_copy_options(block.options),
        )
        self.writer.write_block(new_nrb)


def truncate_stream(instream, outstream, snaplen):
    """
    Read a pcap-ng stream, truncate packet payloads to at most ``snaplen``
    bytes and write the resulting sub-file to the output stream.

    :param instream: a file-like object to read the capture from
    :param outstream: a file-like object to write the truncated capture to
    :param snaplen: maximum number of bytes kept for each packet
    :returns: a list of per-section dicts mapping interface ids to the
        number of packets actually written for that interface
    """
    if snaplen < 1:
        raise ValueError("snaplen must be a positive integer")

    sections_stats = []
    truncator = None
    for block in FileScanner(instream):
        if isinstance(block, blocks.SectionHeader):
            truncator = _SectionTruncator(outstream, block, snaplen)
            sections_stats.append(truncator.delivered)
        else:
            truncator.process(block)
    return sections_stats

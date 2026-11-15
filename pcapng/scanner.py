import struct

import pcapng.blocks as blocks
from pcapng.constants.block_types import BLK_RESERVED, BLK_RESERVED_CORRUPTED
from pcapng.exceptions import CorruptedFile, PcapngLoadError, StreamEmpty
from pcapng.structs import (
    BYTE_ORDER_MAGIC,
    BYTE_ORDER_MAGIC_INVERSE,
    SECTION_HEADER_MAGIC,
    read_block_data,
    read_int,
    read_section_header,
)

SECTION_HEADER_MAGIC_BYTES = struct.pack("<I", SECTION_HEADER_MAGIC)


def _pad32(size):
    """Size of a block payload, including 32-bit alignment padding"""
    return size + (4 - (size % 4)) % 4


class FileScanner(object):
    """
    pcap-ng file scanner.

    This object can be iterated to get blocks out of a pcap-ng
    stream (a file or file-like object providing a .read() method).

    Example usage:

        .. code-block:: python

            from pcapng import FileScanner

            with open('/tmp/mycapture.pcap', 'rb') as fp:
                scanner = FileScanner(fp)
                for block in scanner:
                    pass  # do something with the block...

    :param stream:
        a file-like object from which to read the data.
        If you need to parse data from some string you have entirely in-memory,
        just wrap it in a :py:class:`io.BytesIO` object.
    """

    __slots__ = ["stream", "current_section", "endianness"]

    def __init__(self, stream):
        self.stream = stream
        self.current_section = None
        self.endianness = "="

    def __iter__(self):
        while True:
            try:
                yield self._read_next_block()
            except StreamEmpty:
                return

    def _read_next_block(self):
        block_type = self._read_int(32, False)

        if block_type == SECTION_HEADER_MAGIC:
            block = self._read_section_header()
            self.current_section = block
            self.endianness = block.endianness
            return block

        if self.current_section is None:
            raise ValueError("File not starting with a proper section header")

        block = self._read_block(block_type)

        return block

    def _read_section_header(self):
        """
        Section information headers are special blocks in that they
        modify the state of the FileScanner instance (to change current
        section / endianness)
        """

        section_info = read_section_header(self.stream)
        self.endianness = section_info["endianness"]  # todo: use property?

        # todo: make this use the standard schema facilities as well!
        return blocks.SectionHeader(
            raw=section_info["data"], endianness=section_info["endianness"]
        )

    def _read_block(self, block_type):
        """
        Read the block payload and pass to the appropriate block constructor
        """
        data = read_block_data(self.stream, endianness=self.endianness)

        if block_type in blocks.KNOWN_BLOCKS:
            # This is a known block -- instantiate it
            return self.current_section.new_member(
                blocks.KNOWN_BLOCKS[block_type], raw=data
            )

        if block_type in BLK_RESERVED_CORRUPTED:
            raise CorruptedFile(
                "Block type 0x{0:08X} is reserved to detect a corrupted file".format(
                    block_type
                )
            )

        if block_type == BLK_RESERVED:
            raise CorruptedFile(
                "Block type 0x00000000 is reserved and should not be used "
                "in capture files!"
            )

        return blocks.UnknownBlock(block_type, data)

    def _read_int(self, size, signed=False):
        """
        Read an integer from the stream, using current endianness
        """
        return read_int(self.stream, size, signed=signed, endianness=self.endianness)


class RecoveringFileScanner(object):
    """
    pcap-ng file scanner for possibly-damaged files.

    Iterates like :py:class:`FileScanner`, but a block is yielded only
    once its boundary evidence has been fully verified:

    - the block header (type + total length) was read completely;
    - the declared total length is plausible (not smaller than the
      minimum block size);
    - the whole block, including padding and the trailing total
      length, is actually present in the stream (the archive tail
      may stop in the middle of a block);
    - the trailing total length matches the leading one (neither end
      is trusted on its own);
    - the block content can be decoded;
    - for section headers, the byte-order magic is valid.

    Iteration stops at the first block failing any of these checks.
    Since the section-header magic may also occur inside packet data,
    no attempt is made to resynchronize by scanning for it: everything
    from the failed block up to the end of the stream is reported as
    the first untrusted range.

    After iteration, inspect:

    - :py:attr:`untrusted_range`: ``None`` if the stream ended cleanly
      on a block boundary, otherwise a ``(start, end)`` tuple of
      stream offsets delimiting the first untrusted range;
    - :py:attr:`untrusted_reason`: a string describing which boundary
      evidence failed, or ``None``.

    Example usage:

        .. code-block:: python

            from pcapng import RecoveringFileScanner

            with open('/tmp/damaged.pcapng', 'rb') as fp:
                scanner = RecoveringFileScanner(fp)
                verified = list(scanner)
                print(scanner.untrusted_range, scanner.untrusted_reason)

    :param stream:
        a file-like object from which to read the data.
    """

    MIN_BLOCK_LENGTH = 12
    MIN_SECTION_HEADER_LENGTH = 28

    __slots__ = [
        "stream",
        "offset",
        "current_section",
        "endianness",
        "untrusted_range",
        "untrusted_reason",
    ]

    def __init__(self, stream):
        self.stream = stream
        self.offset = 0
        self.current_section = None
        self.endianness = "="
        self.untrusted_range = None
        self.untrusted_reason = None

    def __iter__(self):
        while self.untrusted_range is None:
            block = self._read_next_block()
            if block is None:
                return
            yield block

    def _read(self, size):
        """Read up to ``size`` bytes, keeping track of the stream offset"""
        data = self.stream.read(size)
        self.offset += len(data)
        return data

    def _reject(self, start, reason):
        """
        Boundary evidence failed at ``start``: mark everything from
        there to the end of the stream as untrusted and stop.
        """
        rest = self.stream.read()
        self.offset += len(rest)
        self.untrusted_range = (start, self.offset)
        self.untrusted_reason = reason
        return None

    def _read_next_block(self):
        start = self.offset
        header = self._read(8)

        if len(header) == 0:
            # Clean end of stream, exactly on a block boundary
            return None

        if len(header) < 8:
            return self._reject(
                start,
                "truncated block header: got {0} of 8 bytes".format(len(header)),
            )

        if header[:4] == SECTION_HEADER_MAGIC_BYTES:
            return self._read_section_header(start, header)

        if self.current_section is None:
            return self._reject(
                start, "file not starting with a proper section header"
            )

        return self._read_block(start, header)

    def _read_section_header(self, start, header):
        bom = self._read(4)
        if len(bom) < 4:
            return self._reject(
                start,
                "truncated section header: got {0} of 12 header bytes".format(
                    8 + len(bom)
                ),
            )

        (byte_order_magic,) = struct.unpack(">I", bom)
        if byte_order_magic == BYTE_ORDER_MAGIC:
            endianness = ">"
        elif byte_order_magic == BYTE_ORDER_MAGIC_INVERSE:
            endianness = "<"
        else:
            return self._reject(
                start,
                "wrong byte order magic: got 0x{0:08X}, expected "
                "0x{1:08X} or 0x{2:08X}".format(
                    byte_order_magic, BYTE_ORDER_MAGIC, BYTE_ORDER_MAGIC_INVERSE
                ),
            )

        (block_length,) = struct.unpack(endianness + "I", header[4:8])
        if block_length < self.MIN_SECTION_HEADER_LENGTH:
            return self._reject(
                start,
                "implausible section header length: {0}".format(block_length),
            )

        # Exclude: magic, len, bom, len (16 bytes); the payload is
        # padded to 32 bits before the trailing length.
        payload_length = block_length - 16
        padded_length = _pad32(payload_length)
        rest = self._read(padded_length + 4)
        if len(rest) < padded_length + 4:
            return self._reject(
                start,
                "truncated section header: declared {0} bytes, "
                "only {1} present".format(block_length, 12 + len(rest)),
            )

        (trailing_length,) = struct.unpack(endianness + "I", rest[padded_length:])
        if trailing_length != block_length:
            return self._reject(
                start,
                "mismatching block lengths: {0} and {1}".format(
                    block_length, trailing_length
                ),
            )

        try:
            block = blocks.SectionHeader(
                raw=rest[:payload_length], endianness=endianness
            )
        except PcapngLoadError as exc:
            return self._reject(start, "section header failed to decode: {0}".format(exc))

        self.current_section = block
        self.endianness = endianness
        return block

    def _read_block(self, start, header):
        (block_type,) = struct.unpack(self.endianness + "I", header[:4])
        (block_length,) = struct.unpack(self.endianness + "I", header[4:8])

        if block_length < self.MIN_BLOCK_LENGTH:
            return self._reject(
                start, "implausible block length: {0}".format(block_length)
            )

        # Exclude: type, len, len (12 bytes); the payload is padded
        # to 32 bits before the trailing length.
        payload_length = block_length - 12
        padded_length = _pad32(payload_length)
        rest = self._read(padded_length + 4)
        if len(rest) < padded_length + 4:
            return self._reject(
                start,
                "truncated block: declared {0} bytes, "
                "only {1} present".format(block_length, 8 + len(rest)),
            )

        (trailing_length,) = struct.unpack(
            self.endianness + "I", rest[padded_length:]
        )
        if trailing_length != block_length:
            return self._reject(
                start,
                "mismatching block lengths: {0} and {1}".format(
                    block_length, trailing_length
                ),
            )

        if block_type in BLK_RESERVED_CORRUPTED:
            return self._reject(
                start,
                "block type 0x{0:08X} is reserved to detect a corrupted "
                "file".format(block_type),
            )

        if block_type == BLK_RESERVED:
            return self._reject(
                start,
                "block type 0x00000000 is reserved and should not be used "
                "in capture files!",
            )

        raw = rest[:payload_length]
        try:
            if block_type in blocks.KNOWN_BLOCKS:
                return self.current_section.new_member(
                    blocks.KNOWN_BLOCKS[block_type], raw=raw
                )
            return blocks.UnknownBlock(block_type, raw)
        except PcapngLoadError as exc:
            return self._reject(
                start, "block failed to decode: {0}".format(exc)
            )

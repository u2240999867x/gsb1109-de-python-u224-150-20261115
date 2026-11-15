import pcapng.blocks as blocks
from pcapng.constants.block_types import BLK_RESERVED, BLK_RESERVED_CORRUPTED
from pcapng.exceptions import CorruptedFile, StreamEmpty
from pcapng.structs import (
    SECTION_HEADER_MAGIC,
    read_block_data,
    read_int,
    read_section_header,
)


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

    Sections are numbered from zero in file order; each section keeps the
    ordered list of the blocks it contains, and every block exposes its
    ``section`` and ``block_index``.
    """

    __slots__ = [
        "stream",
        "current_section",
        "endianness",
        "section_count",
        "blocks",
    ]

    def __init__(self, stream):
        self.stream = stream
        self.current_section = None
        self.endianness = "="
        self.section_count = 0
        self.blocks = []

    def __iter__(self):
        while True:
            try:
                yield self._read_next_block()
            except StreamEmpty:
                return

    def address_table(self, per_section=False):
        """
        Return the address dimension table built from the blocks read so far.

        :param per_section: when true, return one table per section.
        """
        from pcapng.names import build_address_table

        return build_address_table(self.blocks, per_section=per_section)

    @property
    def name_resolution_table(self):
        return self.address_table()

    def _read_next_block(self):
        block_type = self._read_int(32, False)

        if block_type == SECTION_HEADER_MAGIC:
            block = self._read_section_header()
            self.current_section = block
            self.endianness = block.endianness
            self.blocks.append(block)
            return block

        if self.current_section is None:
            raise ValueError("File not starting with a proper section header")

        block = self._read_block(block_type)
        self.blocks.append(block)

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
        section = blocks.SectionHeader(
            raw=section_info["data"], endianness=section_info["endianness"]
        )
        section.section_index = self.section_count
        self.section_count += 1
        return section

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

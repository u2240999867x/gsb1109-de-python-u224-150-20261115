import os
import struct

import pytest

import Evtx.Evtx as evtx

from Evtx.BinaryParser import ParseException


def read_file(data_path, name):
    """
    read the contents of the given test data file.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
      name (str): the name of the test data file.

    Returns:
      bytes: the contents of the file.
    """
    with open(os.path.join(data_path, name), "rb") as f:
        return f.read()


def render_records(chunk, reverse=False):
    """
    render all records of the given chunk, mapping record number to XML.

    Args:
      chunk (Evtx.ChunkHeader): the chunk to render.
      reverse (bool): render the records in reverse order.

    Returns:
      dict[int, str]: the rendered records, keyed by record number.
    """
    records = list(chunk.records())
    if reverse:
        records = records[::-1]
    return {record.record_num(): record.xml() for record in records}


def zero_template_table(buf, chunk_index=0):
    """
    return a copy of the given EVTX file contents with the template table
      of the given chunk zeroed, simulating an offline/dirty chunk whose
      template table was never flushed to disk.

    Args:
      buf (bytes): the EVTX file contents.
      chunk_index (int): the index of the chunk to modify.

    Returns:
      bytes: the modified file contents.
    """
    data = bytearray(buf)
    base = 0x1000 + chunk_index * 0x10000
    for i in range(32):
        struct.pack_into("<I", data, base + 0x180 + i * 4, 0)
    return bytes(data)


def set_record_template_offset(buf, record_offset, template_offset):
    """
    return a copy of the given EVTX file contents with the template offset
      of the record at the given file offset replaced.

    Args:
      buf (bytes): the EVTX file contents.
      record_offset (int): the file offset of the record to modify.
      template_offset (int): the new chunk-relative template offset.

    Returns:
      bytes: the modified file contents.
    """
    data = bytearray(buf)
    # record header is 0x18 bytes, then a 4 byte stream start node,
    #   then the template instance: token (1), unknown (1),
    #   template id (4), template offset (4).
    struct.pack_into("<I", data, record_offset + 0x18 + 0x4 + 0x6, template_offset)
    return bytes(data)


def test_same_template_id_is_isolated_per_chunk(data_path):
    """
    chunk 0 and chunk 5 of system.evtx both define a template with id
      4079676960, but with different definitions (data lengths 1385 and 713).
    each record must be rendered with its own chunk's template,
      regardless of the order in which the chunks are read.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
    """
    buf = read_file(data_path, "system.evtx")

    def render():
        fh = evtx.FileHeader(buf, 0x0)
        chunks = list(fh.chunks())
        rec0 = next(chunks[0].records())
        rec5 = None
        for record in chunks[5].records():
            if record.record_num() == 13049:
                rec5 = record
                break
        # both records use template id 4079676960
        assert rec0.root().template_instance().template_id() == 4079676960
        assert rec5.root().template_instance().template_id() == 4079676960
        return rec0, rec5

    # read chunk 0 before chunk 5
    rec0, rec5 = render()
    xml0_forward = rec0.xml()
    xml5_forward = rec5.xml()

    # read chunk 5 before chunk 0
    rec0, rec5 = render()
    xml5_reverse = rec5.xml()
    xml0_reverse = rec0.xml()

    # each record keeps its own chunk's template fields, in either order
    assert "<EventRecordID>12049</EventRecordID>" in xml0_forward
    assert "<EventRecordID>13049</EventRecordID>" in xml5_forward
    assert xml0_forward == xml0_reverse
    assert xml5_forward == xml5_reverse


def test_chunk_read_order_does_not_affect_results(data_path):
    """
    swapping the order in which two chunks are read must not change
      the rendered events.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
    """
    buf = read_file(data_path, "system.evtx")

    def render_two_chunks(order):
        fh = evtx.FileHeader(buf, 0x0)
        chunks = list(fh.chunks())
        rendered = {}
        for index in order:
            rendered.update(render_records(chunks[index]))
        return rendered

    assert render_two_chunks([0, 1]) == render_two_chunks([1, 0])


def test_record_read_order_does_not_affect_results(data_path):
    """
    templates referenced by a record may be defined inline in another
      record of the same chunk, without an entry in the chunk's template
      table (such as in offline or dirty chunks).
    resolving them must not depend on the order in which records are read.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
    """
    pristine = read_file(data_path, "system.evtx")
    buf = zero_template_table(pristine, chunk_index=0)

    forward = render_records(evtx.FileHeader(buf, 0x0).first_chunk())
    reverse = render_records(evtx.FileHeader(buf, 0x0).first_chunk(), reverse=True)
    assert forward == reverse

    # templates resolved on demand match the pristine file
    assert forward == render_records(evtx.FileHeader(pristine, 0x0).first_chunk())


def test_templates_are_not_shared_between_files(data_path):
    """
    system.evtx and security.evtx both keep a template at chunk-relative
      offset 0x226, with different definitions.
    rendering one file must never reuse the other file's template.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
    """
    system = read_file(data_path, "system.evtx")
    security = read_file(data_path, "security.evtx")

    def first_record_xml(buf):
        fh = evtx.FileHeader(buf, 0x0)
        return next(fh.first_chunk().records()).xml()

    system_xml = first_record_xml(system)
    security_xml = first_record_xml(security)
    assert system_xml != security_xml

    # reading the files in the opposite order must not change either rendering
    assert first_record_xml(security) == security_xml
    assert first_record_xml(system) == system_xml


@pytest.mark.parametrize(
    "template_offset",
    [
        0x10000 + 0x226,  # into the neighboring chunk, which has a template there
        0xFFF0,  # too near the end of the chunk to fit a template header
        0x7FFFFF00,  # far beyond the end of the chunk
    ],
)
def test_out_of_range_template_offset_isolates_only_related_events(data_path, template_offset):
    """
    a record whose template points outside of its chunk is invalid:
      only that event is isolated, and the template is never borrowed
      from a neighboring chunk or another file with the same template id.

    Args:
      data_path (str): the file system path of the test directory. pytest fixture.
      template_offset (int): the chunk-relative template offset to inject.
    """
    buf = read_file(data_path, "system.evtx")
    buf = set_record_template_offset(buf, 0x1000 + 0x200, template_offset)

    fh = evtx.FileHeader(buf, 0x0)
    records = fh.first_chunk().records()
    record = next(records)
    with pytest.raises(ParseException):
        record.xml()

    # the remaining events of the chunk are not affected
    for record in records:
        assert record.xml() is not None

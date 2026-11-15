Library usage
#############

Use the :py:class:`~pcapng.scanner.FileScanner` class to iterate over blocks
in a pcap-ng archive file, like this:

.. code-block:: python

    from pcapng import FileScanner

    with open('/tmp/mycapture.pcap') as fp:
        scanner = FileScanner(fp)
        for block in scanner:
            pass  # do something with the block...

Block types can be checked against blocks in :py:mod:`pcapng.blocks`.

Address names
=============

:py:class:`~pcapng.blocks.NameResolution` blocks carry no timestamp, and tools
may append new ones to an existing capture after the packets were written.  A
name added that way is useful for display, but it does not prove which name was
in use when a packet was captured.

:py:meth:`~pcapng.blocks.SectionHeader.address_table` (or
:py:func:`pcapng.names.build_address_table`) builds an address dimension
table that keeps every declaration and its source:

.. code-block:: python

    from pcapng import FileScanner

    with open('/tmp/mycapture.pcap', 'rb') as fp:
        scanner = FileScanner(fp)
        for block in scanner:
            pass

    table = scanner.address_table()
    entry = table["192.0.2.1"]
    entry.names           # every name, from every block and section
    entry.proven_names    # names that can be tied to a captured packet
    entry.display_aliases # names added after the packets
    entry.declarations    # each with .source, .section and .applies_from

Each declaration records the NRB that declared it and the block position from
which it applies (``applies_from`` / ``applies_until``).  Because NRBs have no
timestamp, the corresponding packet timestamps are exposed separately as
``applies_from_time`` / ``applies_until_time``; they are ``None`` when no
packet exists in that part of the section.

Looking up an address without naming a section does not merge sections: if two
sections declare the same address, ``table.resolve(address)`` returns ``None``
and the caller must choose with ``section=``.  Later declarations never
overwrite earlier ones, and unknown record types keep their raw payload, so
they survive a read/write round trip.

"""
Archive TLS handshake messages from a pcap / pcapng capture.

Unlike a per-packet parser, this example re-assembles each TCP direction by
sequence number before framing TLS records and handshake messages, so it
correctly handles:

* a TLS record spread over several packets,
* several handshake messages inside one record,
* a handshake message spread over several records,
* sequence gaps (bytes after a gap are never stitched together on a
  plausible-looking length field),
* retransmissions (they never create a second handshake entry).

Handshake messages are numbered independently for the two directions of
each connection, and every message lists the TLS records and original
capture packets its bytes came from.

Usage:
    python archive_tls_handshakes.py capture.pcap
"""
from __future__ import print_function

import sys

from dpkt.tls_archive import archive_pcap_file, format_archive


def archive_handshakes(path):
    """Print the archived TLS records and handshake messages of ``path``."""
    connections = archive_pcap_file(path)
    if not connections:
        print('No TCP connections found in %s' % path)
        return
    print(format_archive(connections))

    for number, connection in enumerate(connections, start=1):
        for direction, parser in connection.parsers.items():
            for entry in parser.handshakes.entries:
                if not entry.valid:
                    print('WARNING: connection %d %s handshake #%d (%s) '
                          'framed but not deep-parsed; raw bytes and '
                          'provenance are still archived' % (
                              number, connection.direction_label(direction),
                              entry.index, entry.name))


def test():
    """Run against a capture passed on the command line."""
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    archive_handshakes(sys.argv[1])


if __name__ == '__main__':
    test()

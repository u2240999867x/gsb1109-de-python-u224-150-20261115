"""
Print the two TCP byte ledgers for every flow in a pcap/pcapng capture.

wire_bytes  - payload bytes observed on the wire; every captured segment
              counts, so a retransmitted range is billed each time.
bytes       - distinct payload bytes the application receives for the first
              time; overlapping retransmissions and re-ordered segments are
              de-duplicated by sequence number.

SYN and FIN consume sequence numbers but carry no payload, so they never
appear in either total.  Only bytes actually present in the capture are
counted, so snaplen-truncated frames only testify to the captured part.
"""
import sys

import dpkt
from dpkt.tcpstream import TCPStreamTracker
from dpkt.utils import inet_to_str


def print_tcp_stream_bytes(source):
    """Print the wire and unique-byte totals for each TCP flow."""
    tracker = TCPStreamTracker().update_pcap(source)
    if not tracker.flows:
        print('No TCP traffic found')
        return

    for flow in tracker.flows.values():
        print('%s:%d -> %s:%d  packets=%d' % (
            inet_to_str(flow.src), flow.sport,
            inet_to_str(flow.dst), flow.dport, flow.packets))
        for name, direction in (('c2s', flow.c2s), ('s2c', flow.s2c)):
            print('  %s: segments=%d wire_bytes=%d bytes=%d retrans_bytes=%d'
                  ' syn=%d fin=%d rst=%d' % (
                      name, direction.segments, direction.wire_bytes,
                      direction.bytes, direction.retrans_bytes,
                      direction.syns, direction.fins, direction.rsts))
        print('  total: wire_bytes=%d bytes=%d retrans_bytes=%d\n' % (
            flow.wire_bytes, flow.bytes, flow.retrans_bytes))


def test():
    """Open up a test pcap file and print the TCP byte ledgers"""
    with open('data/http.pcap', 'rb') as f:
        print_tcp_stream_bytes(f)


if __name__ == '__main__':
    for path in sys.argv[1:] or ['data/http.pcap']:
        with open(path, 'rb') as f:
            print_tcp_stream_bytes(f)

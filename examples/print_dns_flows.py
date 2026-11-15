"""
Build a DNS request/response table from an offline capture.

This example correlates DNS messages by endpoints and question identity
rather than transaction ID alone, and shows retransmissions, truncated
responses, TCP fallbacks, and unresolved candidates.
"""
import os
import sys

import dpkt


def print_capture(path):
    with open(path, 'rb') as capture:
        table = dpkt.dnsflow.DNSFlowTable.from_capture(capture)

    dpkt.dnsflow.print_table(table)

    candidates = [row for row in table.table()
                  if row['status'] == dpkt.dnsflow.CANDIDATE]
    if candidates:
        print('\nCandidates (not counted as successful):')
        for row in candidates:
            print('  request %d id=0x%04x %s candidates=%s' %
                  (row['index'], row['id'], row['name'], row['candidates']))


def test():
    """Run against a bundled capture."""
    print_capture(os.path.join(os.path.dirname(__file__), 'data', 'dns_icmp.pcap'))


if __name__ == '__main__':
    if len(sys.argv) > 1:
        print_capture(sys.argv[1])
    else:
        test()

#!/usr/bin/env python

"""
Truncate all packets in a pcap-ng file to a maximum capture length,
keeping original wire lengths and file-level capture statistics, so the
resulting sub-file can be sent to an analysis station.
"""

import argparse

from pcapng.truncate import truncate_stream

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument(
    "snaplen", type=int, help="maximum number of bytes kept for each packet"
)
parser.add_argument("infile", type=argparse.FileType("rb"))
parser.add_argument("outfile", type=argparse.FileType("wb"))
args = parser.parse_args()

truncate_stream(args.infile, args.outfile, args.snaplen)

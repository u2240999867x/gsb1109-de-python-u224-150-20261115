History
#######

Unreleased
==========

- Add section- and position-aware address dimension tables for Name Resolution
  Blocks.  Declarations keep their source block, section and applicability
  range; names added by later blocks are distinguished from names that can be
  proven for a captured packet, all names are retained, and unknown record
  types round-trip unchanged.

2.1.1
=====

- Fix deserialization of custom options

2.1.0
=====

- Use the explicit endianness everywhere when writing.

2.0.0
=====

- Write support

v0.1
====

- Support for "scanning" streams of pcap-ng data
- Support for decoding the "standard" pcap-ng blocks:

  - Section headers
  - Interface description
  - Enhanced packet
  - Simple packet (deprecated)
  - Packet (deprecated)
  - Name resolution
  - Interface statistics

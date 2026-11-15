# Fragment Reassembly

`dpkt.defrag` reassembles fragmented IPv4 and IPv6 datagrams from saved
capture files, offline. It is built for captures taken by more than one
capture card, where unrelated datagrams may share an IP identification value.

## Grouping

A datagram is identified by:

- **capture domain** — the capture file, plus the interface index reported by
  pcapng. Two cards recording the same wire traffic remain separate, so a
  repeated identification value is never merged across cards.
- **direction** — source address to destination address.
- **protocol** — the IPv4 protocol number, or the IPv6 next header that follows
  the fragment header.
- **identification** — the IP identification field.

## Byte accounting

- Fragments are stored by absolute byte range.
- Overlapping ranges must agree byte-for-byte. Any disagreeing byte isolates
  the datagram (`conflict`); it is never emitted as complete.
- A fragment fully covered by accepted bytes is a duplicate and adds no
  effective length. Duplicates still appear in the provenance list.
- The total length comes only from the fragment whose *more fragments* flag is
  clear. If that fragment is missing, the datagram is `incomplete` and
  `total_len` is `None`; the length is never inferred from the other fragments.
- A capture that truncated a fragment leaves the datagram `incomplete`, but the
  IP total-length field still fixes the declared total.
- Fragments must start on an 8-byte boundary, and non-tail fragments must have
  a payload that is a multiple of 8 bytes; violations isolate the datagram.

## Provenance

Every datagram in the report lists the capture records that supplied bytes:

```python
from dpkt.defrag import reassemble_file

report = reassemble_file('card-a.pcapng')
for datagram in report.datagrams:
    print(datagram.state, datagram.total_len)
    for record in datagram.records():
        print(record.domain, record.interface, record.index, record.timestamp, record.role)
```

`record.role` is `payload` for accepted fragments, `duplicate` for replays, and
`conflict` for fragments that disagree with accepted bytes.

## Command line

```bash
python -m dpkt.defrag card-a.pcap card-b.pcapng --json
```

`--json` prints a machine-readable report, `--bytes` adds payload hex, and
`--reorder` sorts records before reassembly (the output set does not change).

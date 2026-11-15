# TLS Handshake Archive Example

This example archives the TLS handshake messages of every TCP connection in
a pcap / pcapng capture, together with the original packets that carried
them. It is implemented in `dpkt/tls_archive.py` and driven from
`examples/archive_tls_handshakes.py`.

## Why three layers

A naive per-packet TLS parser mis-archives most real captures because the
layering does not line up with packet boundaries:

1. **TCP re-assembly by sequence number.** Bytes are only released once
   every preceding sequence number is present. Data sitting behind a gap
   stays queued even when its first bytes look like a valid TLS record, so a
   gap is never silently bridged because a length field "looks reasonable".
   Retransmissions overlap consumed sequence numbers and are merged away, so
   they never produce a second handshake entry.
2. **TLS record framing.** A record may span several TCP segments (and
   therefore several packets), and one segment may contain several records.
3. **TLS handshake framing.** A handshake message may span several records
   (including a split right inside its 4-byte header) and one record may
   contain several handshake messages.

The two directions of a connection are numbered independently.

## Provenance

Every archived entry exposes:

- `entry.records` - the `TLSRecordInfo` objects the message touched,
- `entry.packets` - the sorted 1-based capture packet numbers,
- `entry.origins` - `(packet, tcp payload offset, length)` spans,
- `entry.raw` - the reassembled message bytes,
- `entry.handshake` / `entry.valid` - an optional deep parse via
  `dpkt.ssl.TLSHandshake`; framing is still archived when deep parsing fails.

## Usage

```
python examples/archive_tls_handshakes.py capture.pcap
```

The program prints each connection, its TLS records with packet provenance,
and its numbered handshake messages with the records and packets they span.

**Code Excerpt**

``` python
from dpkt.tls_archive import archive_pcap_file, format_archive

connections = archive_pcap_file('capture.pcap')
print(format_archive(connections))

for connection in connections:
    for direction, parser in connection.parsers.items():
        for entry in parser.handshakes.entries:
            print(connection.direction_label(direction),
                  entry.index, entry.name, entry.packets)
```

# UDP Report

`dpkt.udp_report` summarises UDP payload bytes per source address without
treating capture truncation as packet loss.

## Three byte counts

For each source address the report keeps three independent totals:

| Column | Meaning |
| --- | --- |
| Declared | Payload bytes declared by the UDP length field (`ulen - 8`), or
  derived from the IP length when the zero-length field rules apply. |
| Captured | Payload bytes actually present in the capture and inside the
  declared UDP datagram. This is never inflated. |
| Decodable | Payload bytes from datagrams whose declared payload was captured
  completely and which are not IP fragments. |

Missing payload bytes are reported separately as `Truncated`; they are not
counted as dropped packets.

## Length conflicts

When the IPv4 total length, the IPv6 payload length, the UDP length and the
capture extent disagree, all raw values are kept on the per-packet record
(`report.records`) and a conflict code is attached. The report never
substitutes the largest of the lengths for a missing one.

Conflict codes include:

- `udp_len_exceeds_ip_payload`
- `ip_payload_exceeds_udp_len`
- `ip_truncated_by_capture`
- `capture_truncated_by_snaplen`
- `capture_exceeds_ip_total_len`
- `udp_payload_truncated_by_capture`
- `fragmented_datagram`
- `non_initial_fragment`

Inspect `record.conflict_evidence[code]` for the original field values.

## Zero-length fields

- IPv4 total length `0` means "not supplied" (often TSO); the capture extent is
  used for availability, not as a declaration.
- IPv4 UDP length `0` has no jumbogram meaning; the payload is derived from the
  IPv4 total length.
- IPv6 payload length `0` is a jumbogram marker only when the Hop-by-Hop Jumbo
  Payload option is present.
- IPv6 UDP length `0` is valid only with that jumbo option. Outside a
  jumbogram it is malformed and is not silently replaced with the IPv6 length.

## Usage

```python
import dpkt

report = dpkt.udp_report.analyze('capture.pcap')
print(report.format_text())

for stats in report:
    print(stats.source, stats.declared_payload_bytes,
          stats.captured_payload_bytes, stats.decodable_payload_bytes,
          stats.truncated_payload_bytes)

# Per-packet evidence, including raw IP/UDP length fields
for record in report.records:
    if record.conflicts:
        print(record.as_dict())
```

The command line entry point is:

```bash
python -m dpkt.udp_report capture.pcap
python -m dpkt.udp_report capture.pcap --csv
python -m dpkt.udp_report capture.pcap --json --records
```

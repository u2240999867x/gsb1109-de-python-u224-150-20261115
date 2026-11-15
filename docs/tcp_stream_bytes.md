# TCP Stream Bytes Example

Traffic reports often need to distinguish two numbers for the same TCP
connection:

- **wire bytes** - every payload byte observed on the wire. A retransmitted
  range is billed each time it appears in the capture.
- **unique bytes** - the bytes the application receives for the first time.
  Sequence-number ranges are merged into a union, so overlapping
  retransmissions and re-ordered segments only count once.

`dpkt.tcpstream.TCPStreamTracker` keeps both ledgers per flow direction.
Sequence numbers are linearized so 32-bit wrap-around stays continuous, SYN
and FIN consume sequence numbers but never count as payload, and
snaplen-truncated frames only contribute the bytes actually captured.

**Code Excerpt**

``` python
from dpkt.tcpstream import TCPStreamTracker

tracker = TCPStreamTracker()
with open('http.pcap', 'rb') as f:
    tracker.update_pcap(f)

for flow in tracker.flows.values():
    print(flow.wire_bytes, flow.bytes, flow.retrans_bytes)
```

`flow.c2s` and `flow.s2c` expose the same attributes for each direction,
and `flow.report()` returns a plain dict suitable for serializing into a
daily report.

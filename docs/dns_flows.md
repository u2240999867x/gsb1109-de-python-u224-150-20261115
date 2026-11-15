# DNS Request/Response Correlation

Offline DNS analysis often starts with a naive rule: pair a request and a
response that carry the same transaction ID. That rule silently produces wrong
answers. Clients reuse transaction IDs, queries are retransmitted, and a
truncated UDP response is commonly retried over TCP with a new client port
(sometimes with a new transaction ID).

`dpkt.dnsflow` builds a request/response table from a pcap or pcapng capture
and keeps the evidence for every decision.

## What is considered a match

A response is accepted only when all of these agree with a request:

- the client/server IP address pair,
- the transport (UDP with UDP, TCP with TCP),
- the question section: case-insensitive name, RR type, and class, and
- exactly one open request remains for that identity and endpoint.

The transaction ID is used to find candidates, not as proof. An exact repeat
from the same endpoints and with the same question is treated as a
retransmission when the earlier request is still unanswered. An exact repeat
after the earlier request was answered is a new query, not a retransmission.

## Classifications

Each logical request has a `classification`:

- `new_query`: nothing earlier used the same transaction ID on these
  endpoints.
- `reused_id`: an earlier request used the same transaction ID with a
  different question.
- `retransmission`: the query repeats an unanswered request.
- `tcp_fallback`: a TCP retry after a truncated UDP response.

Request rows use these statuses:

- `matched`: exactly one response was accepted.
- `candidate`: one or more responses were plausible but not conclusive. The
  request stays unmatched so that a weak guess is never reported as success.
- `unanswered`: no response could be attributed.

Responses additionally carry:

- `truncated`: the DNS TC bit is set. The request may legitimately be retried
  over TCP.
- `duplicate`: a later copy of the response already accepted for the same
  request. It is not counted as another success.
- `orphan`: no request with the transaction ID exists in the capture.
- `candidate`: offered to a request but not accepted.

A response object is owned by one request once accepted. If two requests share
a transaction ID, one response can never make both look successful.

## Retransmission and TCP fallback

`DNSRequest.transmissions` lists every attempt:

- `original`: the first transmission.
- `retransmission`: the same question from the same endpoints and transport.
- `tcp_fallback`: a TCP attempt after a truncated UDP response. The TCP
  attempt is linked when it has the same question and there is exactly one
  unanswered UDP request with a truncated response. A changed TCP client port
  is expected; a changed transaction ID is tolerated only under that unique
  truncation evidence.

`DNSRequest.fallbacks` and `DNSRequest.truncated_responses` expose the
supporting packets directly.

## Usage

```python
import dpkt

with open('capture.pcap', 'rb') as f:
    table = dpkt.dnsflow.DNSFlowTable.from_capture(f)

for row in table.table():
    print(row['client'], row['name'], row['classification'], row['status'])
```

`table.table()` returns one row per logical request. `table.message_table()`
returns one row per DNS message for audits of individual retransmissions,
truncated replies, and candidates.

The command line interface prints the same information:

```console
$ python -m dpkt.dnsflow capture.pcap
$ python -m dpkt.dnsflow --messages capture.pcap
$ python -m dpkt.dnsflow --json capture.pcap
```

## Capture edge cases

- pcap and pcapng files are read through `dpkt.pcap.UniversalReader`.
- Ethernet, Linux cooked capture (SLL/SLL2), loopback, raw IP, PPP and PPPoE
  link layers are decoded.
- DNS-over-TCP messages split across segments are reassembled; a reused
  connection is reset on SYN.
- A capture truncated inside a record or inside a DNS message is reported as
  `partial` instead of being treated as a complete answer.
- Responses that appear before their request in an offline capture are retried
  when the request arrives.

## Limits

The capture is the only evidence used. When a response contains no question
section, when its question section is incomplete, or when several open
requests share the same transaction ID, the response is retained as a
`candidate` rather than guessed. The timeout option can additionally expire
old unanswered requests; expired responses stay visible as candidates.

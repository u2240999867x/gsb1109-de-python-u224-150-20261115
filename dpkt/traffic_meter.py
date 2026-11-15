# -*- coding: utf-8 -*-
"""Per-tenant traffic metering for stacked VLAN and tunnel captures.

The module walks the full encapsulation stack of every captured frame
(Ethernet / 802.1Q QinQ / MPLS / IP / IPv6 / GRE / TCP / UDP ...) and keeps:

* the layer-by-layer encapsulation path, together with the on-wire payload
  visible after each layer header;
* the innermost application payload, even when the inner protocol is not
  understood (the frame is still booked on the outer/tenant account);
* the on-wire (capture) length, split once into header overhead and payload,
  so the outer encapsulation cost is never charged again to every inner layer;
* two metering views - one keyed by tenant (outer VLAN id) and one keyed by
  the tenant plus the innermost four-tuple.  Two tenants sharing the same
  private four-tuple therefore never merge into a single flow.
"""
from __future__ import absolute_import

from collections import namedtuple

from . import dpkt
from . import ethernet
from . import gre
from . import ip
from . import ip6
from . import loopback
from . import pcap
from . import sll
from . import sll2
from . import tcp
from . import udp
from .utils import inet_to_str


# A single parsed encapsulation layer.
#
# name:     protocol class name, e.g. 'Ethernet', 'VLANtag8021Q', 'GRE'
# proto:    protocol identifier carried by the layer (ethertype, IP protocol
#           number, GRE protocol type); None when not applicable
# vlan_id:  VLAN id for VLAN layers, None otherwise
# length:   on-wire header length consumed by this layer (its share of the
#           encapsulation overhead, counted exactly once)
# payload:  raw bytes remaining on the wire after this layer header
LayerRecord = namedtuple('LayerRecord', ['name', 'proto', 'vlan_id', 'length', 'payload'])

# Tenant-scoped innermost four-tuple.  The tenant prefix keeps identical
# private four-tuples of different tenants in separate flow entries.
FlowKey = namedtuple('FlowKey', ['tenant', 'src_ip', 'dst_ip', 'src_port', 'dst_port', 'ip_proto'])

# Per-frame audit trail produced by TrafficMeter.meter_packet().
#
# index:     zero-based position in the metering run
# timestamp: capture timestamp when available, else None
# wire_len:  original on-wire length (pcap header 'len' when known)
# cap_len:   number of bytes actually available in the capture buffer
# tenant:    tenant id the frame is booked to
# vlan_ids:  all VLAN ids seen on the frame, outer to inner
# flow_key:  innermost FlowKey, or None when no four-tuple can be extracted
# layers:    tuple of LayerRecord describing the full encapsulation path
# payload:   innermost (application / opaque) payload bytes
PacketRecord = namedtuple(
    'PacketRecord',
    ['index', 'timestamp', 'wire_len', 'cap_len', 'tenant', 'vlan_ids', 'flow_key', 'layers', 'payload']
)


class MeterEntry(object):
    """Cumulative counters for a tenant or a single flow."""

    __slots__ = ('packets', 'bytes', 'payload_bytes', 'overhead_bytes')

    def __init__(self):
        self.packets = 0
        self.bytes = 0
        self.payload_bytes = 0
        self.overhead_bytes = 0

    def add(self, wire_len, overhead, payload_len):
        self.packets += 1
        self.bytes += wire_len
        self.overhead_bytes += overhead
        self.payload_bytes += payload_len

    def as_dict(self):
        return {
            'packets': self.packets,
            'bytes': self.bytes,
            'payload_bytes': self.payload_bytes,
            'overhead_bytes': self.overhead_bytes,
        }

    def __repr__(self):
        return 'MeterEntry(packets=%d, bytes=%d, overhead=%d, payload=%d)' % (
            self.packets, self.bytes, self.overhead_bytes, self.payload_bytes)


_LINKTYPE_DECODERS = {
    pcap.DLT_EN10MB: ethernet.Ethernet,
    pcap.DLT_LINUX_SLL: sll.SLL,
    pcap.DLT_LINUX_SLL2: sll2.SLL2,
    pcap.DLT_LOOP: loopback.Loopback,
    pcap.DLT_NULL: loopback.Loopback,
}


class TrafficMeter(object):
    """Meter captured frames by tenant and by innermost four-tuple.

    Args:
        tenant_vlan: tenant selection policy.  'outer' (default) books a frame
            to its outermost VLAN id, 'inner' to its innermost one, and a
            callable taking the ordered list of VLAN ids may implement any
            other policy.
        default_tenant: tenant id used for frames without any VLAN tag.
    """

    def __init__(self, tenant_vlan='outer', default_tenant=0):
        if tenant_vlan not in ('outer', 'inner') and not callable(tenant_vlan):
            raise ValueError("tenant_vlan must be 'outer', 'inner' or a callable")
        self.tenant_vlan = tenant_vlan
        self.default_tenant = default_tenant
        self.tenant_stats = {}
        self.flow_stats = {}
        self.records = []

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    def meter_packet(self, buf, wire_len=None, linktype=pcap.DLT_EN10MB, timestamp=None):
        """Meter one captured frame and return its PacketRecord.

        Args:
            buf: captured frame bytes (the pcap caplen portion).
            wire_len: original on-wire length from the pcap packet header;
                defaults to len(buf) when the frame was not truncated.
            linktype: data link type of the capture (see pcap.DLT_*).
            timestamp: capture timestamp in seconds, if available.

        A frame whose inner protocol is not understood is still fully metered
        on the tenant account: its outer layers and payload are retained, only
        the innermost four-tuple (flow key) may be missing.
        """
        if not buf:
            raise dpkt.NeedData('empty frame buffer')

        cap_len = len(buf)
        if wire_len is None:
            wire_len = cap_len

        root = self._decode_linklayer(buf, linktype)
        # Slice payloads from the original capture buffer so the metered
        # lengths always reflect the on-wire bytes, never a repacked frame.
        layers, vlan_ids, ip_nodes, consumed, payload = self._walk_stack(root, buf)

        tenant = self._select_tenant(vlan_ids)
        flow_key = self._build_flow_key(tenant, ip_nodes)

        record = PacketRecord(
            index=len(self.records),
            timestamp=timestamp,
            wire_len=wire_len,
            cap_len=cap_len,
            tenant=tenant,
            vlan_ids=tuple(vlan_ids),
            flow_key=flow_key,
            layers=tuple(layers),
            payload=payload,
        )
        self.records.append(record)

        # The captured bytes are partitioned once: every parsed header is
        # counted as overhead a single time, the rest is payload.
        overhead = consumed
        payload_len = cap_len - consumed

        tenant_entry = self.tenant_stats.setdefault(tenant, MeterEntry())
        tenant_entry.add(wire_len, overhead, payload_len)
        if flow_key is not None:
            flow_entry = self.flow_stats.setdefault(flow_key, MeterEntry())
            flow_entry.add(wire_len, overhead, payload_len)
        return record

    def meter_reader(self, reader):
        """Meter every frame yielded by a dpkt pcap/pcapng Reader."""
        linktype = reader.datalink()
        for timestamp, buf in reader:
            self.meter_packet(buf, linktype=linktype, timestamp=timestamp)
        return self.records

    def meter_file(self, path):
        """Meter every frame of a libpcap/pcapng capture file."""
        with open(path, 'rb') as fileobj:
            reader = pcap.UniversalReader(fileobj)
            return self.meter_reader(reader)

    def tenant_report(self):
        """Tenant counters as a list of dicts, sorted by tenant id."""
        return [dict(tenant=tenant, **self.tenant_stats[tenant].as_dict())
                for tenant in sorted(self.tenant_stats)]

    def flow_report(self):
        """Flow counters as a list of dicts, sorted by tenant and four-tuple."""
        rows = []
        for key in sorted(self.flow_stats):
            row = key._asdict()
            row.update(self.flow_stats[key].as_dict())
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _decode_linklayer(buf, linktype):
        decoder = _LINKTYPE_DECODERS.get(linktype)
        if decoder is not None:
            return decoder(buf)
        if linktype == pcap.DLT_RAW:
            return ip6.IP6(buf) if (buf[0] >> 4) == 6 else ip.IP(buf)
        raise ValueError('unsupported linktype %d' % linktype)

    def _walk_stack(self, root, raw):
        """Walk the encapsulation stack of one decoded frame.

        Returns (layers, vlan_ids, ip_nodes, consumed, payload).  'consumed'
        is the total header overhead; each LayerRecord stores the wire bytes
        left after its own header, and the final 'payload' holds every byte
        not consumed by a parsed header (application data, opaque inner
        protocols, padding, trailers, ...).
        """
        layers = []
        vlan_ids = []
        ip_nodes = []
        consumed = 0
        node = root

        while isinstance(node, dpkt.Packet):
            if isinstance(node, ethernet.Ethernet):
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('Ethernet', node.type, None, node.__hdr_len__, b''))
                for tag in getattr(node, 'vlan_tags', None) or []:
                    vlan_id = getattr(tag, 'id', None)
                    if vlan_id is not None:
                        vlan_ids.append(vlan_id)
                    consumed = self._add_layer(
                        layers, raw, consumed,
                        LayerRecord(type(tag).__name__, getattr(tag, 'type', None),
                                    vlan_id, tag.__hdr_len__, b''))
                for label in getattr(node, 'mpls_labels', None) or []:
                    consumed = self._add_layer(
                        layers, raw, consumed,
                        LayerRecord('MPLS', label.val, None, label.__hdr_len__, b''))
                node = node.data

            elif isinstance(node, (sll.SLL, sll2.SLL2)):
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord(type(node).__name__, node.ethtype,
                                                       None, node.__hdr_len__, b''))
                node = node.data

            elif isinstance(node, loopback.Loopback):
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('Loopback', node.family,
                                                       None, node.__hdr_len__, b''))
                node = node.data

            elif isinstance(node, ip.IP):
                header_len = node.hl * 4
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('IP', node.p, None, header_len, b''))
                ip_nodes.append(node)
                node = node.data

            elif isinstance(node, ip6.IP6):
                header_len = node.__hdr_len__ + \
                    sum(len(ext) for ext in getattr(node, 'all_extension_headers', []))
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('IP6', getattr(node, 'p', node.nxt),
                                                       None, header_len, b''))
                ip_nodes.append(node)
                node = node.data

            elif isinstance(node, gre.GRE):
                header_len = len(node) - len(node.data)
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('GRE', node.p, None, header_len, b''))
                node = node.data

            elif isinstance(node, tcp.TCP):
                header_len = node.__hdr_len__ + len(node.opts)
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('TCP', None, None, header_len, b''))
                node = node.data

            elif isinstance(node, udp.UDP):
                consumed = self._add_layer(layers, raw, consumed,
                                           LayerRecord('UDP', None, None, node.__hdr_len__, b''))
                node = node.data

            else:
                # Unknown inner protocol: stop here.  Everything left on the
                # wire is kept as opaque payload and the frame is still booked
                # on the outer/tenant account.
                break

        payload = raw[consumed:]
        return layers, vlan_ids, ip_nodes, consumed, payload

    @staticmethod
    def _add_layer(layers, raw, consumed, record):
        consumed += record.length
        layers.append(record._replace(payload=raw[consumed:]))
        return consumed

    def _select_tenant(self, vlan_ids):
        if not vlan_ids:
            return self.default_tenant
        if self.tenant_vlan == 'outer':
            return vlan_ids[0]
        if self.tenant_vlan == 'inner':
            return vlan_ids[-1]
        return self.tenant_vlan(list(vlan_ids))

    @staticmethod
    def _build_flow_key(tenant, ip_nodes):
        if not ip_nodes:
            return None
        inner_ip = ip_nodes[-1]
        transport = inner_ip.data
        if not isinstance(transport, (tcp.TCP, udp.UDP)):
            # Unknown/opaque inner layer: no four-tuple, but the frame still
            # enters the tenant (outer) account.
            return None
        return FlowKey(
            tenant=tenant,
            src_ip=inet_to_str(inner_ip.src),
            dst_ip=inet_to_str(inner_ip.dst),
            src_port=transport.sport,
            dst_port=transport.dport,
            ip_proto=inner_ip.p,
        )


################################################################################
#                                    TESTS                                     #
################################################################################


def _build_qinq_gre_tcp(outer_vid, inner_vid, inner_src=b'\x0a\x01\x00\x01',
                        inner_dst=b'\x0a\x01\x00\x02', sport=12345, dport=443,
                        payload=b'hello-tenant'):
    """Build Eth / QinQ / IP / GRE / IP / TCP frames for two tenants."""
    segment = tcp.TCP(sport=sport, dport=dport)
    segment.data = payload
    inner = ip.IP(src=inner_src, dst=inner_dst, p=ip.IP_PROTO_TCP, data=segment)
    tunnel = gre.GRE(data=inner)
    outer = ip.IP(src=b'\xc0\xa8\x00\x01', dst=b'\xc0\xa8\x00\x02',
                  p=ip.IP_PROTO_GRE, data=tunnel)
    frame = ethernet.Ethernet(
        dst=b'\x00\x11\x22\x33\x44\x55', src=b'\x66\x77\x88\x99\xaa\xbb',
        vlan_tags=[ethernet.VLANtag8021Q(id=outer_vid),
                   ethernet.VLANtag8021Q(id=inner_vid)],
        data=outer)
    return bytes(frame)


def test_layer_path_and_payload_preserved():
    from .compat import BytesIO

    app_payload = b'keep-every-byte'
    frame = _build_qinq_gre_tcp(100, 10, payload=app_payload)

    meter = TrafficMeter()
    record = meter.meter_packet(frame)

    names = [layer.name for layer in record.layers]
    assert names == ['Ethernet', 'VLANtag8021Q', 'VLANtag8021Q', 'IP', 'GRE', 'IP', 'TCP']

    # VLAN ids and tenant selection (outer tag wins by default)
    assert record.vlan_ids == (100, 10)
    assert record.tenant == 100

    # every layer keeps the wire remainder after its own header
    assert [len(layer.payload) for layer in record.layers] == sorted(
        [len(layer.payload) for layer in record.layers], reverse=True)
    assert record.layers[0].payload == frame[ethernet.ETH_HDR_LEN:]
    assert record.layers[-1].payload == app_payload
    assert record.payload == app_payload

    # on-wire length is partitioned once into overhead + payload
    overhead = sum(layer.length for layer in record.layers)
    assert overhead + len(record.payload) == len(frame)
    assert record.wire_len == record.cap_len == len(frame)

    # the same frame metered through a pcap Reader gives identical numbers
    fileobj = BytesIO()
    writer = pcap.Writer(fileobj, linktype=pcap.DLT_EN10MB)
    writer.writepkt_time(frame, 1.5)
    fileobj.seek(0)
    reader_meter = TrafficMeter()
    reader_meter.meter_reader(pcap.Reader(fileobj))
    assert reader_meter.tenant_stats[100].bytes == len(frame)
    assert reader_meter.records[0].timestamp == 1.5
    assert reader_meter.records[0].payload == app_payload


def test_tenant_and_flow_accounting():
    frame_a = _build_qinq_gre_tcp(100, 10)
    frame_b = _build_qinq_gre_tcp(200, 20)

    meter = TrafficMeter()
    record_a = meter.meter_packet(frame_a)
    record_b = meter.meter_packet(frame_b)

    # identical inner four-tuples must not merge across tenants
    assert record_a.flow_key.tenant == 100
    assert record_b.flow_key.tenant == 200
    assert len(meter.flow_stats) == 2
    assert len(meter.tenant_stats) == 2

    tenant_rows = {row['tenant']: row for row in meter.tenant_report()}
    assert tenant_rows[100]['packets'] == 1
    assert tenant_rows[200]['bytes'] == len(frame_b)

    flow_rows = meter.flow_report()
    assert {row['tenant'] for row in flow_rows} == {100, 200}
    for row in flow_rows:
        assert row['src_ip'] == '10.1.0.1'
        assert row['dst_ip'] == '10.1.0.2'
        assert (row['src_port'], row['dst_port'], row['ip_proto']) == (12345, 443, 6)


def test_overhead_charged_once_per_layer():
    frame = _build_qinq_gre_tcp(100, 10, payload=b'x' * 30)
    meter = TrafficMeter()
    meter.meter_packet(frame)
    meter.meter_packet(frame)

    expected_overhead = sum(layer.length for layer in meter.records[0].layers)
    entry = meter.tenant_stats[100]
    flow_entry = next(iter(meter.flow_stats.values()))

    # tenant and flow are two views of the same bytes, not two allocations
    assert entry.bytes == flow_entry.bytes == 2 * len(frame)
    assert entry.overhead_bytes == flow_entry.overhead_bytes == 2 * expected_overhead
    assert entry.payload_bytes == flow_entry.payload_bytes == 60
    assert entry.overhead_bytes + entry.payload_bytes == entry.bytes


def test_unknown_inner_still_booked_on_outer_account():
    opaque = b'proprietary-inner-protocol'
    outer = ip.IP(src=b'\xc0\xa8\x00\x01', dst=b'\xc0\xa8\x00\x02', p=99, data=opaque)
    frame = bytes(ethernet.Ethernet(
        dst=b'\x00\x11\x22\x33\x44\x55', src=b'\x66\x77\x88\x99\xaa\xbb',
        vlan_tags=[ethernet.VLANtag8021Q(id=300), ethernet.VLANtag8021Q(id=30)],
        data=outer))

    meter = TrafficMeter()
    record = meter.meter_packet(frame)

    # the unknown inner payload survives untouched ...
    assert record.payload == opaque
    assert record.flow_key is None
    names = [layer.name for layer in record.layers]
    assert names == ['Ethernet', 'VLANtag8021Q', 'VLANtag8021Q', 'IP']

    # ... and the frame still enters the outer (tenant) account
    assert record.tenant == 300
    entry = meter.tenant_stats[300]
    assert entry.packets == 1
    assert entry.bytes == len(frame)
    assert entry.overhead_bytes + entry.payload_bytes == len(frame)
    assert meter.flow_stats == {}


def test_inner_vlan_tenant_policy():
    frame = _build_qinq_gre_tcp(100, 10)
    assert TrafficMeter(tenant_vlan='inner').meter_packet(frame).tenant == 10
    assert TrafficMeter(tenant_vlan=lambda ids: ids[0] + ids[1]).meter_packet(frame).tenant == 110
    assert TrafficMeter(default_tenant=7).meter_packet(
        bytes(ethernet.Ethernet(data=ip.IP(p=ip.IP_PROTO_UDP, data=udp.UDP())))).tenant == 7


def test_gre_teb_nested_ethernet():
    app_payload = b'l2-tunnel-payload'
    segment = tcp.TCP(sport=5000, dport=80)
    segment.data = app_payload
    inner_ip = ip.IP(src=b'\x0a\x02\x00\x01', dst=b'\x0a\x02\x00\x02',
                     p=ip.IP_PROTO_TCP, data=segment)
    inner_eth = ethernet.Ethernet(dst=b'\x00\xaa\xbb\xcc\xdd\xee',
                                  src=b'\x00\x11\x22\x33\x44\x55', data=inner_ip)
    tunnel = gre.GRE(p=ethernet.ETH_TYPE_TEB, data=inner_eth)
    outer_ip = ip.IP(src=b'\xc0\xa8\x00\x01', dst=b'\xc0\xa8\x00\x02',
                     p=ip.IP_PROTO_GRE, data=tunnel)
    frame = bytes(ethernet.Ethernet(
        dst=b'\x66\x77\x88\x99\xaa\xbb', src=b'\x00\x11\x22\x33\x44\x55',
        vlan_tags=[ethernet.VLANtag8021Q(id=400)], data=outer_ip))

    record = TrafficMeter().meter_packet(frame)
    names = [layer.name for layer in record.layers]
    assert names == ['Ethernet', 'VLANtag8021Q', 'IP', 'GRE', 'Ethernet', 'IP', 'TCP']
    assert record.tenant == 400
    assert record.payload == app_payload
    assert record.flow_key.src_ip == '10.2.0.1'
    assert record.flow_key.dst_port == 80
    assert sum(layer.length for layer in record.layers) + len(record.payload) == len(frame)


def test_ipv6_inner_four_tuple():
    src6 = b'\x20\x01\x0d\xb8' + b'\x00' * 11 + b'\x01'
    dst6 = b'\x20\x01\x0d\xb8' + b'\x00' * 11 + b'\x02'
    segment = udp.UDP(sport=5353, dport=53)
    segment.data = b'dns-query'
    inner = ip6.IP6(src=src6, dst=dst6, nxt=ip.IP_PROTO_UDP, data=segment)
    frame = bytes(ethernet.Ethernet(
        vlan_tags=[ethernet.VLANtag8021Q(id=500)], data=inner))

    record = TrafficMeter().meter_packet(frame)
    assert record.flow_key.tenant == 500
    assert record.flow_key.src_ip == '2001:db8::1'
    assert record.flow_key.dst_ip == '2001:db8::2'
    assert (record.flow_key.src_port, record.flow_key.dst_port, record.flow_key.ip_proto) == (5353, 53, 17)
    assert record.payload == b'dns-query'


def test_unsupported_linktype_and_empty_buffer():
    import pytest

    meter = TrafficMeter()
    with pytest.raises(ValueError):
        meter.meter_packet(b'\x00' * 10, linktype=0xffff)
    with pytest.raises(dpkt.NeedData):
        meter.meter_packet(b'')

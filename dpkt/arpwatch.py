# -*- coding: utf-8 -*-
"""ARP-based asset catalog.

Build an address inventory from ARP traffic observed in a packet capture.

The catalog is deliberately conservative about what it concludes:

* A binding is a *claim* that an IP address is answered by a particular MAC.
  Every claim carries a validity window (``first_seen`` / ``last_seen`` plus a
  TTL), so callers can tell fresh evidence from stale evidence.
* ARP replies are recorded as evidence even when the answering MAC is a known
  proxy (e.g. an HSRP/VRRP keepalive or a device doing proxy ARP). Proxy
  bindings are flagged, never silently dropped.
* When one MAC answers for several addresses the addresses are kept as
  separate claims. They are **not** merged into a single host, because a proxy
  answers for addresses that belong to distinct machines.
* When several MACs answer for the same IP each ``(ip, mac)`` pair is retained
  and the IP is flagged as an address conflict.
* Not seeing a MAC again does not mean the device went offline. It only means
  no fresh evidence was observed, so the claim ages out via its TTL instead of
  being deleted.
"""
from __future__ import absolute_import

import ipaddress
from collections import namedtuple

from . import arp
from . import dpkt
from . import ethernet
from .compat import BytesIO
from .utils import inet_to_str, mac_to_str

# Default claim lifetime (seconds). ARP caches on common operating systems
# time out on this order; the value is intentionally a parameter, not a truth.
DEFAULT_TTL = 300.0


class Error(dpkt.Error):
    """Base error for the ARP asset catalog."""


class Claim(namedtuple('Claim',
                       ['ip', 'mac', 'first_seen', 'last_seen', 'count',
                        'proxy', 'replies', 'requests'])):
    """Evidence that ``mac`` answers for ``ip``.

    Attributes:
        ip: IPv4 address as a dotted-quad string.
        mac: Ethernet (hardware) address as a colon-hex string.
        first_seen: Timestamp (seconds) of the first observed binding.
        last_seen: Timestamp (seconds) of the most recent observation.
        count: Total ARP packets that asserted this binding.
        proxy: True when the answering MAC is a configured proxy interface.
        replies: Number of ARP replies observed for this binding.
        requests: Number of ARP requests observed for this binding.
    """

    __slots__ = ()

    def is_valid(self, now, ttl=DEFAULT_TTL):
        """Return True if fresh evidence exists for this claim at ``now``.

        A claim is valid while ``now`` is no later than ``last_seen + ttl``.
        Expiry is a query-time notion: the claim is retained so the historical
        evidence is not lost.
        """
        return now <= self.last_seen + ttl

    def expires_at(self, ttl=DEFAULT_TTL):
        """Return the timestamp at which this claim becomes stale."""
        return self.last_seen + ttl

    def as_dict(self):
        """Return the claim as a plain, JSON-serialisable dictionary."""
        return {
            'ip': self.ip,
            'mac': self.mac,
            'first_seen': self.first_seen,
            'last_seen': self.last_seen,
            'count': self.count,
            'proxy': self.proxy,
            'replies': self.replies,
            'requests': self.requests,
        }


class AssetCatalog(object):
    """Accumulate ARP claims from observed packets or a capture file.

    Args:
        scope: Optional collection of IPv4 ranges to include. Each entry may be
            a single address (``'10.0.0.1'``) or a network
            (``'10.0.0.0/24'``). Addresses outside the scope are ignored.
        proxies: Optional collection of proxy-interface MAC addresses, in any
            form accepted by :func:`normalize_mac`. Replies from these MACs are
            retained and flagged rather than treated as ordinary owners.
        ttl: Validity duration (seconds) applied when querying active claims.
    """

    def __init__(self, scope=None, proxies=None, ttl=DEFAULT_TTL):
        self.ttl = float(ttl)
        self._networks = self._parse_scope(scope)
        self._proxies = frozenset(normalize_mac(m) for m in (proxies or []))

        # key: (ip_str, mac_str) -> mutable record dict
        self._claims = {}
        # key: ip_str -> set(mac_str)
        self._ip_macs = {}
        # key: mac_str -> set(ip_str)
        self._mac_ips = {}

    # ------------------------------------------------------------------ #
    # Input configuration
    # ------------------------------------------------------------------ #
    @staticmethod
    def _parse_scope(scope):
        if not scope:
            return None
        networks = []
        for entry in scope:
            net = ipaddress.ip_network(_to_text(entry), strict=False)
            if net.version != 4:
                raise Error('only IPv4 ranges are supported: %r' % (entry,))
            networks.append(net)
        return networks

    def _in_scope(self, ip_int):
        if self._networks is None:
            return True
        addr = ipaddress.IPv4Address(ip_int)
        return any(addr in net for net in self._networks)

    # ------------------------------------------------------------------ #
    # Observation API
    # ------------------------------------------------------------------ #
    def observe_packet(self, buf, ts):
        """Record the ARP evidence contained in one Ethernet frame.

        Args:
            buf: Raw Ethernet frame bytes (as yielded by a pcap reader).
            ts: Capture timestamp in seconds.

        Returns:
            The updated :class:`Claim`, or ``None`` if the frame carried no
            in-scope ARP evidence.
        """
        try:
            eth = ethernet.Ethernet(buf)
        except dpkt.UnpackError:
            return None
        if not isinstance(eth.data, arp.ARP):
            return None
        return self.observe_arp(eth.data, ts)

    def observe_arp(self, pkt, ts):
        """Record one parsed ARP packet.

        Returns:
            The updated :class:`Claim`, or ``None`` when the packet is not an
            IPv4/Ethernet ARP request or reply inside the configured scope.
        """
        if not isinstance(pkt, arp.ARP):
            return None
        if pkt.hrd != arp.ARP_HRD_ETH or pkt.pro != arp.ARP_PRO_IP:
            return None
        if pkt.hln != 6 or pkt.pln != 4:
            return None
        if pkt.op not in (arp.ARP_OP_REQUEST, arp.ARP_OP_REPLY):
            return None
        if len(pkt.spa) != 4 or len(pkt.sha) != 6:
            return None

        spa_int = _bytes_to_int(pkt.spa)
        # 0.0.0.0 is used while an address is still being claimed (gratuitous
        # ARP / duplicate detection) and does not identify a usable address.
        if spa_int == 0:
            return None
        if not self._in_scope(spa_int):
            return None

        ip_str = inet_to_str(pkt.spa)
        mac_str = mac_to_str(pkt.sha)
        is_reply = pkt.op == arp.ARP_OP_REPLY
        return self._record(ip_str, mac_str, ts, is_reply)

    def _record(self, ip_str, mac_str, ts, is_reply):
        key = (ip_str, mac_str)
        record = self._claims.get(key)
        if record is None:
            record = {
                'ip': ip_str,
                'mac': mac_str,
                'first_seen': ts,
                'last_seen': ts,
                'count': 0,
                'proxy': mac_str in self._proxies,
                'replies': 0,
                'requests': 0,
            }
            self._claims[key] = record
            self._ip_macs.setdefault(ip_str, set()).add(mac_str)
            self._mac_ips.setdefault(mac_str, set()).add(ip_str)

        record['last_seen'] = ts
        record['count'] += 1
        if is_reply:
            record['replies'] += 1
        else:
            record['requests'] += 1
        return self._to_claim(record)

    def read_pcap(self, fileobj):
        """Consume every packet from an open pcap or pcapng file object."""
        from . import pcap
        reader = pcap.UniversalReader(fileobj)
        for ts, buf in reader:
            self.observe_packet(buf, ts)
        return self

    def read_pcap_file(self, path):
        """Open and consume a capture file by path."""
        with open(path, 'rb') as fileobj:
            return self.read_pcap(fileobj)

    # ------------------------------------------------------------------ #
    # Query API
    # ------------------------------------------------------------------ #
    def _to_claim(self, record):
        return Claim(**record)

    def claims(self, active_only=False, now=None, ttl=None):
        """Return all recorded claims, oldest first-seen first.

        Args:
            active_only: When True, only claims still valid at ``now`` are
                returned. Expired claims are otherwise included so historical
                evidence remains visible.
            now: Reference timestamp for validity; defaults to the latest
                observed timestamp.
            ttl: Override the catalog TTL for this query.
        """
        ttl = self.ttl if ttl is None else float(ttl)
        if now is None:
            now = self.last_timestamp
        ordered = sorted(self._claims.values(),
                         key=lambda r: (r['first_seen'], r['ip'], r['mac']))
        result = [self._to_claim(r) for r in ordered]
        if active_only:
            result = [c for c in result if c.is_valid(now, ttl)]
        return result

    @property
    def last_timestamp(self):
        """Most recent timestamp fed to the catalog (0 if empty)."""
        if not self._claims:
            return 0.0
        return max(r['last_seen'] for r in self._claims.values())

    def macs_for_ip(self, ip_str):
        """Return every MAC that claimed ``ip_str`` (proxy or not)."""
        return sorted(self._ip_macs.get(_to_text(ip_str), set()))

    def ips_for_mac(self, mac_str):
        """Return every address ``mac_str`` was observed answering for.

        The addresses are returned individually; they are not coalesced into a
        single host because a proxy MAC answers for several real devices.
        """
        return sorted(self._mac_ips.get(normalize_mac(mac_str), set()),
                      key=_ip_sort_key)

    def conflicts(self):
        """Return IPs claimed by more than one MAC with their claim sets."""
        result = {}
        for ip_str in sorted(self._ip_macs, key=_ip_sort_key):
            macs = self._ip_macs[ip_str]
            if len(macs) > 1:
                records = sorted((self._claims[(ip_str, mac)] for mac in macs),
                                 key=lambda r: r['first_seen'])
                result[ip_str] = [self._to_claim(r) for r in records]
        return result

    def proxy_claims(self):
        """Return the claims answered by a configured proxy interface."""
        records = sorted((r for r in self._claims.values() if r['proxy']),
                         key=lambda r: (r['first_seen'], r['ip'], r['mac']))
        return [self._to_claim(r) for r in records]

    def devices(self):
        """Return observed MAC interfaces with their distinct address claims.

        A proxy interface keeps one entry per answered address instead of the
        addresses being folded together, which preserves the one-claim-per-
        binding invariant.
        """
        return {
            mac: sorted(ips, key=_ip_sort_key)
            for mac, ips in sorted(self._mac_ips.items())
        }

    def active_claims(self, now=None, ttl=None):
        """Convenience accessor for claims still within their validity."""
        return self.claims(active_only=True, now=now, ttl=ttl)


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
def _bytes_to_int(raw):
    value = 0
    for byte in raw:
        value = (value << 8) | (byte if isinstance(byte, int) else ord(byte))
    return value


def _to_text(value):
    if isinstance(value, bytes):
        return value.decode('ascii')
    return value


def _ip_sort_key(ip_str):
    return tuple(int(part) for part in _to_text(ip_str).split('.'))


def normalize_mac(mac):
    """Normalise a MAC address to the canonical ``aa:bb:..`` lowercase form.

    Accepts raw 6-byte values or strings using ``:``, ``-``, ``.`` or no
    separator (e.g. Cisco ``aabb.ccdd.eeff``).
    """
    if isinstance(mac, bytes) and len(mac) == 6:
        return mac_to_str(mac)

    text = _to_text(mac).strip().lower()
    cleaned = text.replace(':', '').replace('-', '').replace('.', '')
    if len(cleaned) != 12:
        raise Error('invalid MAC address: %r' % (mac,))
    try:
        int(cleaned, 16)
    except ValueError:
        raise Error('invalid MAC address: %r' % (mac,))
    return ':'.join(cleaned[i:i + 2] for i in range(0, 12, 2))


def build_arp_packet(op, sha, spa, tha, tpa):
    """Construct a raw Ethernet frame carrying an ARP packet (for tests)."""
    arp_pkt = arp.ARP(
        op=op,
        sha=sha,
        spa=spa,
        tha=tha,
        tpa=tpa,
    )
    return bytes(ethernet.Ethernet(
        dst=b'\xff\xff\xff\xff\xff\xff',
        src=sha,
        type=ethernet.ETH_TYPE_ARP,
        data=arp_pkt,
    ))


def catalog_from_pcap(fileobj, scope=None, proxies=None, ttl=DEFAULT_TTL):
    """Convenience helper returning a populated :class:`AssetCatalog`."""
    catalog = AssetCatalog(scope=scope, proxies=proxies, ttl=ttl)
    return catalog.read_pcap(fileobj)


def catalog_from_pcap_bytes(data, scope=None, proxies=None, ttl=DEFAULT_TTL):
    """Build a catalog from an in-memory libpcap byte string (for tests)."""
    return catalog_from_pcap(BytesIO(data), scope=scope, proxies=proxies,
                             ttl=ttl)


################################################################################
#                                    TESTS                                     #
################################################################################

def _ip(text):
    import socket
    return socket.inet_aton(text)


def _mac(last):
    return b'\x00' * 5 + bytes(bytearray([last]))


HOST_A = _mac(0x11)
PROXY = _mac(0x01)
CONFLICT_1 = _mac(0x30)
CONFLICT_2 = _mac(0x40)
STALE = _mac(0x50)
ZERO_MAC = b'\x00' * 6


def _build_pcap(events):
    from . import pcap
    buf = BytesIO()
    writer = pcap.Writer(buf, linktype=pcap.DLT_EN10MB)
    for ts, frame in events:
        writer.writepkt(frame, ts=ts)
    return buf.getvalue()


def _reply(sha, ip_text):
    return build_arp_packet(arp.ARP_OP_REPLY, sha, _ip(ip_text),
                            ZERO_MAC, _ip('10.0.0.2'))


def _request(sha, ip_text, target_text):
    return build_arp_packet(arp.ARP_OP_REQUEST, sha, _ip(ip_text),
                            ZERO_MAC, _ip(target_text))


def _make_catalog(ttl=DEFAULT_TTL, proxies=('00:00:00:00:00:01',),
                  scope=('10.0.0.0/24',)):
    events = [
        # ordinary device: one request then a reply for its own address
        (10.0, _request(HOST_A, '10.0.0.11', '10.0.0.2')),
        (10.5, _reply(HOST_A, '10.0.0.11')),
        # one proxy MAC answers for two distinct addresses
        (20.0, _reply(PROXY, '10.0.0.21')),
        (21.0, _reply(PROXY, '10.0.0.22')),
        # two MACs answer the same address -> conflict
        (30.0, _reply(CONFLICT_1, '10.0.0.30')),
        (35.0, _reply(CONFLICT_2, '10.0.0.30')),
        # a device seen once, early, then never again
        (40.0, _reply(STALE, '10.0.0.50')),
        # out-of-scope address must be ignored
        (50.0, _reply(HOST_A, '192.168.1.1')),
        # 0.0.0.0 sender (duplicate/gratuitous detection) carries no address
        (60.0, build_arp_packet(arp.ARP_OP_REQUEST, HOST_A, _ip('0.0.0.0'),
                                ZERO_MAC, _ip('10.0.0.11'))),
        # refresh host A much later so the capture extends past the stale TTL
        (1000.0, _reply(HOST_A, '10.0.0.11')),
    ]
    return catalog_from_pcap_bytes(_build_pcap(events), scope=scope,
                                   proxies=proxies, ttl=ttl)


def test_normalize_mac():
    assert normalize_mac(b'\xaa\xbb\xcc\xdd\xee\xff') == 'aa:bb:cc:dd:ee:ff'
    assert normalize_mac('AA:BB:CC:DD:EE:FF') == 'aa:bb:cc:dd:ee:ff'
    assert normalize_mac('aabb.ccdd.eeff') == 'aa:bb:cc:dd:ee:ff'
    assert normalize_mac('aa-bb-cc-dd-ee-ff') == 'aa:bb:cc:dd:ee:ff'
    assert normalize_mac('aabbccddeeff') == 'aa:bb:cc:dd:ee:ff'


def test_normalize_mac_invalid():
    import pytest
    for bad in ('nope', 'aa:bb', b'\x01\x02'):
        with pytest.raises(Error):
            normalize_mac(bad)


def test_scope_filters_claims():
    catalog = _make_catalog()
    ips = {claim.ip for claim in catalog.claims()}
    assert '192.168.1.1' not in ips
    # every surviving address is inside 10.0.0.0/24
    assert all(ip.startswith('10.0.0.') for ip in ips)


def test_single_host_scope():
    catalog = AssetCatalog(scope=('10.0.0.11',))
    catalog.observe_arp(
        arp.ARP(op=arp.ARP_OP_REPLY, sha=HOST_A, spa=_ip('10.0.0.11'),
                tha=ZERO_MAC, tpa=_ip('10.0.0.2')), 1.0)
    catalog.observe_arp(
        arp.ARP(op=arp.ARP_OP_REPLY, sha=PROXY, spa=_ip('10.0.0.21'),
                tha=ZERO_MAC, tpa=_ip('10.0.0.2')), 1.0)
    assert [c.ip for c in catalog.claims()] == ['10.0.0.11']


def test_claim_counts_and_evidence():
    catalog = _make_catalog()
    claim = next(c for c in catalog.claims()
                 if c.ip == '10.0.0.11' and c.mac == mac_to_str(HOST_A))
    assert claim.requests == 1
    assert claim.replies == 2
    assert claim.count == 3
    assert claim.first_seen == 10.0
    assert claim.last_seen == 1000.0
    assert claim.proxy is False


def test_proxy_replies_are_retained_and_flagged():
    catalog = _make_catalog()
    proxy_mac = mac_to_str(PROXY)
    proxy = catalog.proxy_claims()
    assert {c.ip for c in proxy} == {'10.0.0.21', '10.0.0.22'}
    assert all(c.mac == proxy_mac and c.proxy for c in proxy)


def test_proxy_addresses_are_not_merged():
    catalog = _make_catalog()
    proxy_mac = mac_to_str(PROXY)
    # the proxy answers two addresses but they remain distinct claims
    answered = catalog.ips_for_mac(proxy_mac)
    assert answered == ['10.0.0.21', '10.0.0.22']
    keys = {(c.ip, c.mac) for c in catalog.claims()}
    assert ('10.0.0.21', proxy_mac) in keys
    assert ('10.0.0.22', proxy_mac) in keys
    # the device inventory keeps one address per binding, not a merged host
    assert catalog.devices()[proxy_mac] == ['10.0.0.21', '10.0.0.22']


def test_conflict_keeps_every_mac():
    catalog = _make_catalog()
    conflicts = catalog.conflicts()
    assert list(conflicts) == ['10.0.0.30']
    macs = sorted(c.mac for c in conflicts['10.0.0.30'])
    assert macs == sorted([mac_to_str(CONFLICT_1), mac_to_str(CONFLICT_2)])
    assert catalog.macs_for_ip('10.0.0.30') == macs
    # both bindings still exist as ordinary claims
    assert len([c for c in catalog.claims() if c.ip == '10.0.0.30']) == 2


def test_absence_is_expiry_not_offline():
    catalog = _make_catalog(ttl=300.0)
    stale_mac = mac_to_str(STALE)
    # the historical claim is never deleted merely because it stopped appearing
    all_claims = catalog.claims()
    stale = next(c for c in all_claims
                 if c.ip == '10.0.0.50' and c.mac == stale_mac)
    assert stale.last_seen == 40.0

    now = catalog.last_timestamp  # 1000.0
    assert stale.is_valid(now, 300.0) is False
    assert stale.expires_at(300.0) == 340.0

    active = catalog.active_claims(now=now)
    assert (stale.ip, stale.mac) not in {(c.ip, c.mac) for c in active}
    # but it is still present in the full, evidence-preserving view
    assert (stale.ip, stale.mac) in {(c.ip, c.mac) for c in all_claims}


def test_ttl_override_and_fresh_claim():
    catalog = _make_catalog(ttl=300.0)
    now = catalog.last_timestamp
    # a generous TTL keeps the old claim valid without altering its evidence
    assert catalog.active_claims(now=now, ttl=10000.0)
    host_a = next(c for c in catalog.claims() if c.ip == '10.0.0.11')
    assert host_a.is_valid(now, 300.0) is True
    assert host_a.expires_at(300.0) == 1300.0


def test_non_arp_and_truncated_frames_ignored():
    catalog = AssetCatalog()
    ip_frame = bytes(ethernet.Ethernet(
        dst=ZERO_MAC, src=HOST_A, type=ethernet.ETH_TYPE_IP,
        data=b'\x45\x00\x00\x14'))
    assert catalog.observe_packet(ip_frame, 1.0) is None
    assert catalog.observe_packet(b'\x00\x01', 1.0) is None
    assert catalog.claims() == []


def test_non_ethernet_arp_ignored():
    catalog = AssetCatalog()
    pkt = arp.ARP(hrd=arp.ARP_HRD_IEEE802, op=arp.ARP_OP_REPLY,
                  sha=HOST_A, spa=_ip('10.0.0.11'), tha=ZERO_MAC,
                  tpa=_ip('10.0.0.2'))
    assert catalog.observe_arp(pkt, 1.0) is None
    assert catalog.claims() == []


def test_as_dict_is_serializable():
    catalog = _make_catalog()
    claim = catalog.claims()[0]
    data = claim.as_dict()
    assert data['ip'] == claim.ip
    assert set(data) == set(claim._fields)


def test_round_trip_through_pcap():
    catalog = _make_catalog()
    # five distinct in-scope addresses survived the pcap write/read cycle
    assert {c.ip for c in catalog.claims()} == {
        '10.0.0.11', '10.0.0.21', '10.0.0.22', '10.0.0.30', '10.0.0.50'}


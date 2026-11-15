import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from binascii import crc32
from collections import OrderedDict

import vdf
from vdf import vbkv_audit


def make_vbkv(obj, checksum_delta=0, magic=b'VBKV'):
    payload = b''.join(vdf._binary_dump_gen(obj, alt_format=True))
    checksum = (crc32(payload) + checksum_delta) & 0xffffffff
    return magic + struct.pack('<I', checksum) + payload, payload


SAMPLE = OrderedDict([
    ('app', OrderedDict([
        ('name', 'demo'),
        ('count', 3),
        ('nested', OrderedDict([('flag', 1)])),
    ])),
])


class AuditBytes(unittest.TestCase):
    def test_valid_container_is_trusted(self):
        blob, payload = make_vbkv(SAMPLE)
        result = vbkv_audit.audit_bytes(blob, source='ok.vbkv')

        self.assertTrue(result['header']['magic_ok'])
        self.assertTrue(result['header']['header_complete'])
        self.assertEqual(result['header']['payload_offset'], [8, len(blob)])

        self.assertTrue(result['checksum']['match'])
        self.assertEqual(result['checksum']['actual_unsigned'],
                         zlib.crc32(payload) & 0xffffffff)
        self.assertEqual(result['checksum']['stored_unsigned'],
                         result['checksum']['actual_unsigned'])

        self.assertTrue(result['body']['structurally_complete'])
        self.assertTrue(result['body']['library_parse_ok'])
        self.assertEqual(result['body']['parsed_preview'], dict(SAMPLE))

        self.assertTrue(result['trusted'])
        self.assertEqual(result['catalog_action'], 'accept')
        self.assertEqual(result['reasons'], [])

    def test_bad_checksum_intact_body_is_quarantined(self):
        blob, payload = make_vbkv(SAMPLE, checksum_delta=1)
        result = vbkv_audit.audit_bytes(blob)

        self.assertFalse(result['checksum']['match'])
        self.assertIn('checksum mismatch', result['reasons'])
        self.assertTrue(result['body']['structurally_complete'])
        self.assertTrue(result['body']['library_parse_ok'])
        self.assertIn('untrusted', result['body']['preview_trust'])
        self.assertFalse(result['trusted'])
        self.assertEqual(result['catalog_action'], 'quarantine')

        # the real library refuses exactly this container
        with self.assertRaises(ValueError):
            vdf.vbkv_loads(blob)

        # yet the payload itself parses independently, matching the premise:
        # parseable body, untrusted container
        self.assertEqual(
            vdf.binary_loads(blob[8:], alt_format=True), dict(SAMPLE))

    def test_recomputation_is_independent_of_stored_value(self):
        blob, payload = make_vbkv(SAMPLE, checksum_delta=0xDEADBEEF)
        result = vbkv_audit.audit_bytes(blob)

        self.assertNotEqual(result['checksum']['stored_unsigned'],
                            result['checksum']['actual_unsigned'])
        self.assertEqual(
            result['checksum']['actual_unsigned'],
            zlib.crc32(blob[8:]) & 0xffffffff)
        self.assertEqual(
            result['checksum']['payload_sha256'],
            hashlib.sha256(payload).hexdigest())
        self.assertEqual(result['checksum']['checksummed_range'],
                         [8, len(blob)])

    def test_corrupted_payload_is_reported_under_body(self):
        blob, _ = make_vbkv(SAMPLE)
        broken = bytearray(blob)
        # payload starts at byte 8; overwrite its first type token with an
        # unknown type so both checksum and structure fail independently
        broken[8] = 0x33
        result = vbkv_audit.audit_bytes(bytes(broken))

        self.assertFalse(result['trusted'])
        self.assertFalse(result['checksum']['match'])
        self.assertIn('structural errors in body', result['reasons'])
        self.assertFalse(result['body']['structurally_complete'])
        self.assertFalse(result['body']['library_parse_ok'])
        self.assertIsNotNone(result['body']['library_parse_error'])

    def test_bad_magic_and_truncation(self):
        bad_magic, _ = make_vbkv(SAMPLE, magic=b'XXXX')
        result = vbkv_audit.audit_bytes(bad_magic)
        self.assertFalse(result['header']['magic_ok'])
        self.assertFalse(result['trusted'])
        self.assertIn('bad magic', result['reasons'])

        short = vbkv_audit.audit_bytes(b'VBKV\x01')
        self.assertFalse(short['header']['header_complete'])
        self.assertIn('truncated header', short['reasons'])
        self.assertEqual(short['catalog_action'], 'quarantine')

    def test_high_bit_crc_library_quirk(self):
        # find a payload whose correct CRC32 has the high bit set
        payload, checksum = b'', 0
        for seed in range(100000):
            candidate = b''.join(
                vdf._binary_dump_gen({'k': str(seed)}, alt_format=True))
            checksum = crc32(candidate) & 0xffffffff
            if checksum >= 0x80000000:
                payload = candidate
                break
        else:
            self.fail('could not find high-bit CRC fixture')

        blob = b'VBKV' + struct.pack('<I', checksum) + payload
        result = vbkv_audit.audit_bytes(blob)

        # unsigned, semantic CRC comparison: correct
        self.assertTrue(result['checksum']['match'])
        # library compares signed int against unsigned CRC and rejects
        self.assertFalse(result['checksum']['library_accepts_checksum'])
        with self.assertRaises(ValueError):
            vdf.vbkv_loads(blob)


class ReadOnlyBehavior(unittest.TestCase):
    def test_audit_file_never_modifies_input(self):
        blob, _ = make_vbkv(SAMPLE, checksum_delta=7)
        with tempfile.NamedTemporaryFile(delete=False) as fp:
            fp.write(blob)
            path = fp.name
        try:
            before = os.stat(path)
            digest_before = hashlib.sha256(open(path, 'rb').read()).hexdigest()

            result = vbkv_audit.audit_file(path)
            self.assertFalse(result['trusted'])

            after = os.stat(path)
            digest_after = hashlib.sha256(open(path, 'rb').read()).hexdigest()
            self.assertEqual(digest_before, digest_after)
            self.assertEqual(before.st_size, after.st_size)
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            self.assertEqual(open(path, 'rb').read(), blob)
        finally:
            os.unlink(path)


class StructureScan(unittest.TestCase):
    def test_counts_and_nesting(self):
        blob, _ = make_vbkv(SAMPLE)
        scan = vbkv_audit.scan_body_structure(blob[8:])
        self.assertTrue(scan['structurally_complete'])
        self.assertGreaterEqual(scan['max_depth'], 2)
        self.assertGreater(scan['nodes'], 0)
        self.assertIn('end_alt', scan['type_counts'])
        self.assertEqual(scan['trailing_bytes'], 0)
        self.assertEqual(scan['errors'], [])

    def test_truncated_body_collects_error(self):
        blob, _ = make_vbkv(SAMPLE)
        scan = vbkv_audit.scan_body_structure(blob[8:-2])
        self.assertFalse(scan['structurally_complete'])
        self.assertTrue(scan['errors'])

    def test_standard_end_marker_rejected_in_alt_payload(self):
        # 0x08 end marker instead of alt 0x0B
        scan = vbkv_audit.scan_body_structure(b'\x01a\x00b\x00\x08')
        self.assertFalse(scan['structurally_complete'])


class CliAndReport(unittest.TestCase):
    def test_json_report_sections_are_separate(self):
        blob, _ = make_vbkv(SAMPLE, checksum_delta=1)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.vbkv') as fp:
            fp.write(blob)
            path = fp.name
        try:
            proc = subprocess.run(
                [sys.executable, '-m', 'vdf.vbkv_audit', path, '--json'],
                capture_output=True, text=True, cwd=os.getcwd())
            self.assertEqual(proc.returncode, 1)
            report = json.loads(proc.stdout)[0]
            self.assertEqual(set(report),
                             {'source', 'trusted', 'reasons', 'catalog_action',
                              'header', 'checksum', 'body'})
        finally:
            os.unlink(path)

    def test_text_report_mentions_three_sections_and_no_repair(self):
        blob, _ = make_vbkv(SAMPLE)
        text = vbkv_audit.render_text_report(
            vbkv_audit.audit_bytes(blob, source='sample.vbkv'))
        for marker in ('[头部 Header]', '[校验和 Checksum]',
                       '[正文结构 Body]', '只读', '不修复'):
            self.assertIn(marker, text)

    def test_missing_file_exit_code(self):
        proc = subprocess.run(
            [sys.executable, '-m', 'vdf.vbkv_audit', '/no/such/file.vbkv'],
            capture_output=True, text=True, cwd=os.getcwd())
        self.assertEqual(proc.returncode, 2)


if __name__ == '__main__':
    unittest.main()

import os
import tempfile
import unittest
from unittest import mock

import numpy as np

import nrrd
from nrrd.errors import NRRDError


class TestNrrdStore(unittest.TestCase):
    def setUp(self):
        self.store_dir = tempfile.mkdtemp('nrrdstoretest')
        self.store = nrrd.NrrdStore(self.store_dir)
        self.data = np.arange(30, dtype=np.int32).reshape((2, 3, 5))

    def _data_files(self):
        return os.listdir(os.path.join(self.store_dir, 'data'))

    def test_publish_and_read_current(self):
        version = self.store.publish(self.data)

        self.assertEqual(version, 1)
        self.assertEqual(self.store.current_version(), 1)

        data, header = self.store.read()
        np.testing.assert_array_equal(data, self.data)
        self.assertEqual(header['type'], 'int32')

    def test_read_with_plain_nrrd(self):
        self.store.publish(self.data)

        data, _ = nrrd.read(self.store.header_path())
        np.testing.assert_array_equal(data, self.data)

    def test_unmodified_data_file_is_reused(self):
        self.store.publish(self.data)
        self.store.publish(self.data)

        self.assertEqual(self.store.versions(), [1, 2])
        self.assertEqual(len(self._data_files()), 1)

    def test_modified_data_gets_new_file_and_old_version_stays_readable(self):
        self.store.publish(self.data)

        modified_data = self.data + 1
        self.store.publish(modified_data)

        self.assertEqual(len(self._data_files()), 2)

        # The old version is still readable and unchanged
        old_data, _ = self.store.read(version=1)
        np.testing.assert_array_equal(old_data, self.data)

        new_data, _ = self.store.read()
        np.testing.assert_array_equal(new_data, modified_data)

    def test_referenced_data_file_is_never_overwritten_in_place(self):
        self.store.publish(self.data)

        data_file = os.path.join(self.store_dir, 'data', self._data_files()[0])
        with open(data_file, 'rb') as fh:
            content_before = fh.read()

        self.store.publish(self.data + 1)

        with open(data_file, 'rb') as fh:
            self.assertEqual(fh.read(), content_before)

    def test_interrupted_publish_keeps_old_version_visible(self):
        self.store.publish(self.data)

        with mock.patch.object(self.store, '_commit_current', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.store.publish(self.data + 1)

        # Old version is still the visible one and reads fine
        self.assertEqual(self.store.current_version(), 1)
        data, _ = self.store.read()
        np.testing.assert_array_equal(data, self.data)

        # A later publish recovers and becomes visible
        version = self.store.publish(self.data + 1)
        self.assertEqual(self.store.current_version(), version)
        data, _ = self.store.read()
        np.testing.assert_array_equal(data, self.data + 1)

    def test_interrupted_publish_leaves_no_visible_partial_version(self):
        self.store.publish(self.data)

        with mock.patch.object(self.store, '_stage_header', side_effect=RuntimeError('simulated crash')):
            with self.assertRaises(RuntimeError):
                self.store.publish(self.data + 1)

        # Orphaned data file from the failed publish is collected, referenced file is kept
        self.assertEqual(len(self._data_files()), 2)
        removed = self.store.collect_garbage()

        self.assertEqual(len(removed), 1)
        self.assertEqual(len(self._data_files()), 1)

        data, _ = self.store.read()
        np.testing.assert_array_equal(data, self.data)

    def test_collect_garbage_keeps_files_referenced_by_any_version(self):
        self.store.publish(self.data)
        self.store.publish(self.data + 1)
        self.store.publish(self.data + 2)

        self.assertEqual(len(self._data_files()), 3)
        removed = self.store.collect_garbage()

        # All three files are still referenced by some version, nothing is removed
        self.assertEqual(removed, [])
        self.assertEqual(len(self._data_files()), 3)

        # Delete an old version, its data file becomes collectable
        self.store.delete_version(1)
        removed = self.store.collect_garbage()

        self.assertEqual(len(removed), 1)
        self.assertEqual(len(self._data_files()), 2)

        # Remaining versions still read fine
        data, _ = self.store.read(version=2)
        np.testing.assert_array_equal(data, self.data + 1)

    def test_delete_current_version_is_rejected(self):
        self.store.publish(self.data)

        with self.assertRaises(NRRDError):
            self.store.delete_version(1)

    def test_read_without_publish_raises(self):
        with self.assertRaises(NRRDError):
            self.store.read()

    def test_read_unknown_version_raises(self):
        self.store.publish(self.data)

        with self.assertRaises(NRRDError):
            self.store.read(version=42)

    def test_raw_encoding(self):
        self.store.publish(self.data, {'encoding': 'raw'})

        self.assertTrue(self._data_files()[0].endswith('.raw'))

        data, _ = self.store.read()
        np.testing.assert_array_equal(data, self.data)

    def test_store_is_relocatable(self):
        self.store.publish(self.data)

        moved_dir = tempfile.mkdtemp('nrrdstoretest') + '_moved'
        os.rename(self.store_dir, moved_dir)

        moved_store = nrrd.NrrdStore(moved_dir)
        data, _ = moved_store.read()
        np.testing.assert_array_equal(data, self.data)


if __name__ == '__main__':
    unittest.main()

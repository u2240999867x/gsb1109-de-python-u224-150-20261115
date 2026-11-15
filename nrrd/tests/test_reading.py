import bz2
import gzip
import io
import unittest
import warnings
from tempfile import TemporaryDirectory
from typing import ClassVar

import numpy as np
from typing_extensions import Literal

import nrrd
from nrrd.tests.util import *


class Abstract:
    class TestReadingFunctions(unittest.TestCase):
        index_order: ClassVar[Literal['F', 'C']]

        def setUp(self):
            self.expected_header = {'dimension': 3,
                                    'encoding': 'raw',
                                    'endian': 'little',
                                    'kinds': ['domain', 'domain', 'domain'],
                                    'sizes': np.array([30, 30, 30]),
                                    'space': 'left-posterior-superior',
                                    'space directions': np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
                                    'space origin': np.array([0, 0, 0]),
                                    'type': 'short'}

            self.expected_data = np.fromfile(RAW_DATA_FILE_PATH, np.int16).reshape((30, 30, 30))
            if self.index_order == 'F':
                self.expected_data = self.expected_data.T

        def test_read_header_only(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)

            # np.testing.assert_equal is used to compare the headers because it will appropriately handle each
            # value in the structure. Since some values can be Numpy arrays inside the headers, this must be used to
            # compare the two values.
            np.testing.assert_equal(self.expected_header, header)

        def test_read_header_only_with_filename(self):
            header = nrrd.read_header(RAW_NRRD_FILE_PATH)

            # np.testing.assert_equal is used to compare the headers because it will appropriately handle each
            # value in the structure. Since some values can be Numpy arrays inside the headers, this must be used to
            # compare the two values.
            np.testing.assert_equal(self.expected_header, header)

        def test_read_detached_header_only(self):
            expected_header = self.expected_header
            expected_header['data file'] = os.path.basename(RAW_DATA_FILE_PATH)

            with open(RAW_NHDR_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)

            np.testing.assert_equal(self.expected_header, header)

        def test_read_header_and_data_filename(self):
            data, header = nrrd.read(RAW_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_detached_header_and_data(self):
            expected_header = self.expected_header
            expected_header['data file'] = os.path.basename(RAW_DATA_FILE_PATH)

            data, header = nrrd.read(RAW_NHDR_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_detached_header_and_data_with_byteskip_minus1(self):
            expected_header = self.expected_header
            expected_header['data file'] = os.path.basename(RAW_DATA_FILE_PATH)
            expected_header['byte skip'] = -1

            data, header = nrrd.read(RAW_BYTESKIP_NHDR_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_detached_header_and_nifti_data_with_byteskip_minus1(self):
            expected_header = self.expected_header
            expected_header['data file'] = os.path.basename(RAW_DATA_FILE_PATH)
            expected_header['byte skip'] = -1
            expected_header['encoding'] = 'gzip'
            expected_header['data file'] = 'BallBinary30x30x30.nii.gz'

            data, header = nrrd.read(GZ_BYTESKIP_NIFTI_NHDR_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_detached_header_and_nifti_data(self):
            with self.assertRaisesRegex(
                    nrrd.NRRDError,
                    'Size of the data does not equal the product of all the dimensions: 27000-27176=-176'):
                nrrd.read(GZ_NIFTI_NHDR_FILE_PATH, index_order=self.index_order)

        def test_read_detached_header_and_data_with_byteskip_minus5(self):
            with self.assertRaisesRegex(nrrd.NRRDError,
                                        'Invalid byteskip, allowed values are greater than or equal to -1'):
                nrrd.read(RAW_INVALID_BYTESKIP_NHDR_FILE_PATH, index_order=self.index_order)

        def test_read_header_and_gz_compressed_data(self):
            expected_header = self.expected_header
            expected_header['encoding'] = 'gzip'

            data, header = nrrd.read(GZ_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_header_and_gz_compressed_data_with_byteskip_minus1(self):
            expected_header = self.expected_header
            expected_header['encoding'] = 'gzip'
            expected_header['type'] = 'int16'
            expected_header['byte skip'] = -1

            data, header = nrrd.read(GZ_BYTESKIP_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_header_and_bz2_compressed_data(self):
            expected_header = self.expected_header
            expected_header['encoding'] = 'bzip2'

            data, header = nrrd.read(BZ2_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_header_and_gz_compressed_data_with_lineskip3(self):
            expected_header = self.expected_header
            expected_header['encoding'] = 'gzip'
            expected_header['line skip'] = 3

            data, header = nrrd.read(GZ_LINESKIP_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(self.expected_header, header)
            np.testing.assert_equal(data, self.expected_data)

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_raw_header(self):
            expected_header = {'type': 'float', 'dimension': 3, 'min': 0, 'max': 35.4}
            header = nrrd.read_header(('NRRD0005', 'type: float', 'dimension: 3', 'min: 0', 'max: 35.4'))
            self.assertEqual(expected_header, header)

            expected_header = {'my extra info': 'my : colon-separated : values'}
            header = nrrd.read_header(('NRRD0005', 'my extra info:=my : colon-separated : values'))
            np.testing.assert_equal(expected_header, header)

        def test_read_dup_field_error_and_warn(self):
            expected_header = {'type': 'float', 'dimension': 3}
            header_txt_tuple = ('NRRD0005', 'type: float', 'dimension: 3', 'type: float')

            with self.assertRaisesRegex(nrrd.NRRDError, 'Duplicate header field: type'):
                header = nrrd.read_header(header_txt_tuple)

            import warnings
            with warnings.catch_warnings(record=True) as w:
                nrrd.reader.ALLOW_DUPLICATE_FIELD = True
                header = nrrd.read_header(header_txt_tuple)

                self.assertTrue('Duplicate header field: type' in str(w[0].message))

                self.assertEqual(expected_header, header)
                nrrd.reader.ALLOW_DUPLICATE_FIELD = False

        def test_read_header_and_ascii_1d_data(self):
            expected_header = {'dimension': 1,
                               'encoding': 'ASCII',
                               'kinds': ['domain'],
                               'sizes': [27],
                               'spacings': [1.0458000000000001],
                               'type': 'unsigned char'}

            data, header = nrrd.read(ASCII_1D_NRRD_FILE_PATH, index_order=self.index_order)

            self.assertEqual(header, expected_header)
            np.testing.assert_equal(data.dtype, np.uint8)
            np.testing.assert_equal(data, np.arange(1, 28))

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_header_and_ascii_2d_data(self):
            expected_header = {'dimension': 2,
                               'encoding': 'ASCII',
                               'kinds': ['domain', 'domain'],
                               'sizes': [3, 9],
                               'spacings': [1.0458000000000001, 2],
                               'type': 'unsigned short'}

            data, header = nrrd.read(ASCII_2D_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(header, expected_header)
            np.testing.assert_equal(data.dtype, np.uint16)

            expected_shape = (3, 9) if self.index_order == 'F' else (9, 3)
            np.testing.assert_equal(data, np.arange(1, 28).reshape(expected_shape, order=self.index_order))

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_read_simple_4d_nrrd(self):
            expected_header = {'type': 'double',
                               'dimension': 4,
                               'space': 'right-anterior-superior',
                               'sizes': np.array([1, 1, 1, 1]),
                               'space directions': np.array([[1.5, 0., 0.],
                                                             [0., 1.5, 0.],
                                                             [0., 0., 1.],
                                                             [np.nan, np.nan, np.nan]]),
                               'endian': 'little',
                               'encoding': 'raw',
                               'measurement frame': np.array([[1.0001, 0., 0.],
                                                              [0., 1.0000000006, 0.],
                                                              [0., 0., 1.000000000000009]])}

            data, header = nrrd.read(RAW_4D_NRRD_FILE_PATH, index_order=self.index_order)

            np.testing.assert_equal(header, expected_header)
            np.testing.assert_equal(data.dtype, np.float64)
            np.testing.assert_equal(header['measurement frame'].dtype, np.float64)
            np.testing.assert_equal(data, np.array([[[[0.76903426]]]]))

            # Test that the data read is able to be edited
            self.assertTrue(data.flags['WRITEABLE'])

        def test_custom_fields_without_field_map(self):
            expected_header = {'dimension': 1,
                               'encoding': 'ASCII',
                               'kinds': ['domain'],
                               'sizes': [27],
                               'spacings': [1.0458000000000001],
                               'int': '24',
                               'double': '25.5566',
                               'string': 'This is a long string of information that is important.',
                               'int list': '1 2 3 4 5 100',
                               'double list': '0.2 0.502 0.8',
                               'string list': 'words are split by space in list',
                               'int vector': '(100, 200, -300)',
                               'double vector': '(100.5,200.3,-300.99)',
                               'int matrix': '(1,0,0) (0,1,0) (0,0,1)',
                               'double matrix': '(1.2,0.3,0) (0,1.5,0) (0,-0.55,1.6)',
                               'type': 'unsigned char'}

            header = nrrd.read_header(ASCII_1D_CUSTOM_FIELDS_FILE_PATH)

            self.assertEqual(header, expected_header)

        def test_custom_fields_with_field_map(self):
            expected_header = {'dimension': 1,
                               'encoding': 'ASCII',
                               'kinds': ['domain'],
                               'sizes': [27],
                               'spacings': [1.0458000000000001],
                               'int': 24,
                               'double': 25.5566,
                               'string': 'This is a long string of information that is important.',
                               'int list': np.array([1, 2, 3, 4, 5, 100]),
                               'double list': np.array([0.2, 0.502, 0.8]),
                               'string list': ['words', 'are', 'split', 'by', 'space', 'in', 'list'],
                               'int vector': np.array([100, 200, -300]),
                               'double vector': np.array([100.5, 200.3, -300.99]),
                               'int matrix': np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1]]),
                               'double matrix': np.array([[1.2, 0.3, 0.0], [0.0, 1.5, 0.0], [0.0, -0.55, 1.6]]),
                               'type': 'unsigned char'}

            custom_field_map = {'int': 'int',
                                'double': 'double',
                                'string': 'string',
                                'int list': 'int list',
                                'double list': 'double list',
                                'string list': 'string list',
                                'int vector': 'int vector',
                                'double vector': 'double vector',
                                'int matrix': 'int matrix',
                                'double matrix': 'double matrix'}
            header = nrrd.read_header(ASCII_1D_CUSTOM_FIELDS_FILE_PATH, custom_field_map)

            np.testing.assert_equal(header, expected_header)

        def test_invalid_custom_field(self):
            custom_field_map = {'int': 'fake'}

            with self.assertRaisesRegex(nrrd.NRRDError, 'Invalid field type given: fake'):
                nrrd.read_header(ASCII_1D_CUSTOM_FIELDS_FILE_PATH, custom_field_map)

        def test_invalid_magic_line(self):
            with self.assertRaisesRegex(nrrd.NRRDError, 'Invalid NRRD magic line. Is this an NRRD file?'):
                nrrd.read_header(('invalid magic line', 'my extra info:=my : colon-separated : values'))

        def test_invalid_magic_line2(self):
            with self.assertRaisesRegex(nrrd.NRRDError,
                                        'Unsupported NRRD file version \\(version: 2000\\). This library '
                                        'only supports v5 and below.'):
                nrrd.read_header(('NRRD2000', 'my extra info:=my : colon-separated : values'))

        def test_invalid_magic_line3(self):
            with self.assertRaisesRegex(nrrd.NRRDError, 'Invalid NRRD magic line: NRRDnono'):
                nrrd.read_header(('NRRDnono', 'my extra info:=my : colon-separated : values'))

        def test_missing_required_field(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Delete required field
                del header['type']

                with self.assertRaisesRegex(nrrd.NRRDError, 'Header is missing required field: type'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_wrong_sizes(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Make the number of dimensions wrong
                header['dimension'] = 2

                with self.assertRaisesRegex(nrrd.NRRDError, 'Number of elements in sizes does not match dimension. '
                                                            'Dimension: 2, len\\(sizes\\): 3'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_nonpositive_dimensions(self):
            cases = [(0, []), (-1, []), (1, [0]), (1, [-1]),
                     (2, [2, 0]), (2, [2, -1]), (2, [-2, -3])]
            for dimension, sizes in cases:
                with self.subTest(dimension=dimension, sizes=sizes):
                    header = {'type': 'uint8', 'encoding': 'raw',
                              'dimension': dimension, 'sizes': np.array(sizes, dtype=int)}
                    with io.BytesIO(b'\x01\x02\x03\x04\x05\x06') as fh:
                        with self.assertRaises(nrrd.NRRDError):
                            nrrd.read_data(header, fh, index_order=self.index_order)
                        self.assertEqual(fh.tell(), 0)

        def test_nonpositive_dimensions_before_datatype_lookup(self):
            for dimension, sizes in [(0, []), (1, [0]), (1, [-1])]:
                with self.subTest(dimension=dimension, sizes=sizes):
                    header = {'type': 'invalid', 'encoding': 'raw',
                              'dimension': dimension, 'sizes': np.array(sizes, dtype=int)}
                    with io.BytesIO(b'\x01') as fh:
                        with self.assertRaisesRegex(nrrd.NRRDError, 'must be positive'):
                            nrrd.read_data(header, fh, index_order=self.index_order)

        def test_nonpositive_dimensions_before_detached_file_access(self):
            with TemporaryDirectory() as directory:
                filename = os.path.join(directory, 'header.nhdr')
                for dimension, sizes in [(0, []), (1, [0]), (1, [-1])]:
                    with self.subTest(dimension=dimension, sizes=sizes):
                        header = {'type': 'uint8', 'encoding': 'raw', 'data file': 'missing.raw',
                                  'dimension': dimension, 'sizes': np.array(sizes, dtype=int)}
                        with self.assertRaisesRegex(nrrd.NRRDError, 'must be positive'):
                            nrrd.read_data(header, filename=filename, index_order=self.index_order)

        def test_positive_dimensions(self):
            expected_2d = [[1, 3, 5], [2, 4, 6]] if self.index_order == 'F' else [[1, 2], [3, 4], [5, 6]]
            cases = [([1], b'\x07', [7]), ([2, 3], b'\x01\x02\x03\x04\x05\x06', expected_2d)]
            for sizes, payload, expected in cases:
                with self.subTest(sizes=sizes):
                    header = {'type': 'uint8', 'encoding': 'raw',
                              'dimension': len(sizes), 'sizes': np.array(sizes)}
                    with io.BytesIO(payload) as fh:
                        data = nrrd.read_data(header, fh, index_order=self.index_order)
                    np.testing.assert_equal(data, expected)
                    np.testing.assert_equal(data.dtype, np.uint8)

        def test_invalid_encoding(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Set the encoding to be incorrect
                header['encoding'] = 'fake'

                with self.assertRaisesRegex(nrrd.NRRDError, 'Unsupported encoding: fake'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_detached_header_no_filename(self):
            self.expected_header['data file'] = os.path.basename(RAW_DATA_FILE_PATH)

            with open(RAW_NHDR_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # No filename is specified for read_data
                with self.assertRaisesRegex(nrrd.NRRDError,
                                            'Filename parameter must be specified when a relative data file'
                                            ' path is given'):
                    nrrd.read_data(header, fh)

        def test_invalid_lineskip(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Set the line skip to be incorrect
                header['line skip'] = -1

                with self.assertRaisesRegex(nrrd.NRRDError,
                                            'Invalid lineskip, allowed values are greater than or equal to'
                                            ' 0'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_missing_endianness(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Delete the endian field from header
                # Since our data is short (itemsize = 2), we should receive an error
                del header['endian']

                with self.assertRaisesRegex(nrrd.NRRDError, 'Header is missing required field: endian'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_big_endian(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Set endianness to big to verify it is doing correctly
                header['endian'] = 'big'

                data = nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)
                np.testing.assert_equal(data, self.expected_data.byteswap())

        def test_invalid_endian(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                np.testing.assert_equal(self.expected_header, header)

                # Set endianness to fake value
                header['endian'] = 'fake'

                with self.assertRaisesRegex(nrrd.NRRDError, 'Invalid endian value in header: fake'):
                    nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH)

        def test_invalid_index_order(self):
            with self.assertRaisesRegex(nrrd.NRRDError, 'Invalid index order'):
                nrrd.read(RAW_NRRD_FILE_PATH, index_order=None)

        def test_read_quoted_string_header(self):
            header = nrrd.read_header([
                'NRRD0004',
                '# Complete NRRD file format specification at:',
                '# http://teem.sourceforge.net/nrrd/format.html',
                'type: double',
                'dimension: 3',
                'space dimension: 3',
                'sizes: 32 40 16',
                'encoding: raw',
                'units: "mm" "cm" "in"',
                'space units: "mm" "cm" "in"',
                'labels: "X" "Y" "f(log(X, 10), Y)"',
                'space origin: (-0.79487200000000002,-1,-0.38461499999999998)'
            ])

            # Check that the quoted values were appropriately parsed
            self.assertEqual(['mm', 'cm', 'in'], header['units'])
            self.assertEqual(['mm', 'cm', 'in'], header['space units'])
            self.assertEqual(['X', 'Y', 'f(log(X, 10), Y)'], header['labels'])

        def test_read_quoted_string_header_no_quotes(self):
            header = nrrd.read_header([
                'NRRD0004',
                '# Complete NRRD file format specification at:',
                '# http://teem.sourceforge.net/nrrd/format.html',
                'type: double',
                'dimension: 3',
                'space dimension: 3',
                'sizes: 32 40 16',
                'encoding: raw',
                'units: mm cm in',
                'space units: mm cm in',
                'labels: X Y f(log(X,10),Y)',
                'space origin: (-0.79487200000000002,-1,-0.38461499999999998)'
            ])

            # Check that the quoted values were appropriately parsed
            self.assertEqual(['mm', 'cm', 'in'], header['units'])
            self.assertEqual(['mm', 'cm', 'in'], header['space units'])
            self.assertEqual(['X', 'Y', 'f(log(X,10),Y)'], header['labels'])

        def test_read_memory(self):
            def test(filename: str):
                with open(filename, 'rb') as fh:
                    # Read into BytesIO and test that
                    x = fh.read()
                    memory_file = io.BytesIO(x)
                    memory_header = nrrd.read_header(memory_file)
                    memory_data = nrrd.read_data(memory_header, memory_file)

                    # Read normally via file handle
                    fh.seek(0)
                    expected_header = nrrd.read_header(fh)
                    expected_data = nrrd.read_data(expected_header, fh)

                    np.testing.assert_equal(expected_header, memory_header)
                    np.testing.assert_equal(expected_data, memory_data)

                    # Test that the data read is able to be edited
                    self.assertTrue(memory_data.flags['WRITEABLE'])

            paths = [
                RAW_NRRD_FILE_PATH,
                GZ_NRRD_FILE_PATH,
                GZ_BYTESKIP_NRRD_FILE_PATH,
                GZ_LINESKIP_NRRD_FILE_PATH,
                BZ2_NRRD_FILE_PATH,
                ASCII_1D_NRRD_FILE_PATH,
                ASCII_2D_NRRD_FILE_PATH,
                RAW_4D_NRRD_FILE_PATH,
            ]

            for filename in paths:
                with self.subTest(filename):
                    test(filename)

        def test_read_space_directions_list(self):
            try:
                nrrd.SPACE_DIRECTIONS_TYPE = 'double vector list'

                _, header = nrrd.read(RAW_4D_NRRD_FILE_PATH, index_order=self.index_order)
                self.assertIsInstance(header['space directions'], list)
                self.assertTrue(
                    all(vector is None or isinstance(vector, np.ndarray) for vector in header['space directions']))
                np.testing.assert_equal(header['space directions'][0].dtype, np.float64)
                np.testing.assert_equal(header['space directions'], [np.array([1.5, 0., 0.]),
                                        np.array([0., 1.5, 0.]),
                                        np.array([0., 0., 1.]),
                                        None])
            finally:
                nrrd.SPACE_DIRECTIONS_TYPE = 'double matrix'

        def test_file_parameter_not_closed(self):
            with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
                header = nrrd.read_header(fh)
                nrrd.read_data(header, fh)
                self.assertFalse(fh.closed)


class _CountingBytesIO(io.BytesIO):
    """BytesIO that records how many bytes are actually handed to the caller."""

    def __init__(self, data):
        super().__init__(data)
        self.bytes_read = 0
        self.read_calls = 0

    def read(self, size=-1):
        chunk = super().read(size)
        self.bytes_read += len(chunk)
        self.read_calls += 1
        return chunk


class TestReadRegion(unittest.TestCase):
    def setUp(self):
        self.data_f, self.header_f = nrrd.read(RAW_NRRD_FILE_PATH, index_order='F')
        self.data_c, self.header_c = nrrd.read(RAW_NRRD_FILE_PATH, index_order='C')

    def _slice(self, region):
        slices = []
        for axis in region:
            if axis is None:
                slices.append(slice(None))
            elif isinstance(axis, int):
                slices.append(slice(axis, axis + 1))
            elif isinstance(axis, tuple):
                slices.append(slice(axis[0], axis[1] + 1))
            else:
                slices.append(axis)
        return tuple(slices)

    def test_region_matches_full_read(self):
        cases = [
            [(2, 5), (0, 29), (10, 20)],
            [slice(2, 6), slice(3, 29), slice(10, 21)],
            [(5, 5), None, (0, 29)],
            [None, None, None],
            [0, 0, 0],
            [(0, 29), (0, 29), (0, 0)],
            [(0, 0), (0, 29), (0, 29)],
            [(29, 29), (29, 29), (29, 29)],
            [slice(1, 29, 1), slice(2, 29, 1), slice(3, 29, 1)],
        ]
        with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
            header = nrrd.read_header(fh)
            data_start = fh.tell()
            for region in cases:
                with self.subTest(index_order='F', region=region):
                    fh.seek(data_start)
                    actual = nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH, 'F', region)
                    np.testing.assert_array_equal(actual, self.data_f[self._slice(region)])

                reversed_region = tuple(reversed(region))
                with self.subTest(index_order='C', region=reversed_region):
                    fh.seek(data_start)
                    actual = nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH, 'C', reversed_region)
                    np.testing.assert_array_equal(actual, self.data_c[self._slice(reversed_region)])

    def test_region_is_transposed_for_c_order(self):
        data, header, info = nrrd.read(RAW_NRRD_FILE_PATH, index_order='C', region=[(10, 20), (0, 29), (2, 5)])
        np.testing.assert_array_equal(data, self.data_c[10:21, :, 2:6])
        self.assertEqual((11, 30, 4), data.shape)
        np.testing.assert_array_equal(header['sizes'], [4, 30, 11])
        self.assertEqual((11, 30, 4), info.shape)
        self.assertEqual((10, 0, 2), info.starts)
        self.assertEqual((20, 29, 5), info.stops)
        self.assertEqual('C', info.index_order)
        self.assertFalse(info.full_decode)

    def test_region_range_and_header(self):
        data, header, info = nrrd.read(RAW_NRRD_FILE_PATH, region=[(2, 5), (0, 29), (10, 20)])
        np.testing.assert_array_equal(data, self.data_f[2:6, :, 10:21])
        np.testing.assert_array_equal(header['sizes'], [4, 30, 11])
        # Space origin is shifted by the number of removed voxels along each spatial axis.
        np.testing.assert_allclose(header['space origin'], [2, 0, 10])
        self.assertEqual((30, 30, 30), info.sizes)
        self.assertEqual((2, 0, 10), info.starts)
        self.assertEqual((5, 29, 20), info.stops)
        self.assertEqual((4, 30, 11), info.shape)
        self.assertEqual((2, 0, 10), info.to_original((0, 0, 0)))
        self.assertEqual((5, 29, 20), info.to_original((3, 29, 10)))

    def test_raw_region_reads_only_selected_bytes(self):
        raw = open(RAW_NRRD_FILE_PATH, 'rb').read()
        stream = _CountingBytesIO(raw)
        header = nrrd.read_header(stream)
        start = stream.bytes_read

        data = nrrd.read_data(header, stream, RAW_NRRD_FILE_PATH, 'F', [(2, 5), (0, 29), (10, 20)])

        # The selected block spans 4 rows x 11 columns of the (x, y) plane for every z in range, i.e. 330 disjoint
        # 4-value runs. Only those bytes may be read.
        self.assertEqual(data.size * data.dtype.itemsize, stream.bytes_read - start)
        self.assertLess(stream.bytes_read - start, 30 * 30 * 30 * data.dtype.itemsize)

    def test_raw_region_coalesces_fully_selected_fast_axes(self):
        raw = open(RAW_NRRD_FILE_PATH, 'rb').read()
        stream = _CountingBytesIO(raw)
        header = nrrd.read_header(stream)
        start = stream.bytes_read

        data = nrrd.read_data(header, stream, RAW_NRRD_FILE_PATH, 'F', [(0, 29), (0, 29), (5, 5)])

        # x and y are fully covered, so a single contiguous read per slice suffices.
        self.assertEqual(1, stream.read_calls)
        self.assertEqual(data.size * data.dtype.itemsize, stream.bytes_read - start)

    def test_region_on_non_cubic_data(self):
        with TemporaryDirectory() as directory:
            filename = os.path.join(directory, 'non_cubic.nrrd')
            shape_f = (7, 5, 3)
            data_f = np.arange(np.prod(shape_f), dtype=np.int32).reshape(shape_f[::-1]).T
            nrrd.write(filename, data_f, {'encoding': 'raw'}, index_order='F')

            data_c = nrrd.read(filename, index_order='C')[0]

            for region in [[(1, 3), (0, 4), (2, 2)], [(3, 6), (1, 2), None], [(6, 6), (4, 4), (2, 2)]]:
                with self.subTest(index_order='F', region=region):
                    actual = nrrd.read(filename, region=region)[0]
                    expected = data_f[tuple(slice(axis[0], axis[1] + 1) if isinstance(axis, tuple) else slice(None)
                                            for axis in region)]
                    np.testing.assert_array_equal(actual, expected)

            for region in [[(0, 2), (0, 4), (1, 3)], [(2, 2), (3, 4), (0, 6)], [(2, 2), (4, 4), (6, 6)]]:
                with self.subTest(index_order='C', region=region):
                    actual = nrrd.read(filename, index_order='C', region=region)[0]
                    expected = data_c[tuple(slice(axis[0], axis[1] + 1) if isinstance(axis, tuple) else slice(None)
                                            for axis in region)]
                    np.testing.assert_array_equal(actual, expected)

    def test_region_axis_forms(self):
        with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
            header = nrrd.read_header(fh)
            data_start = fh.tell()

            # A 2-tuple is an inclusive (start, stop) range.
            fh.seek(data_start)
            actual = nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH, 'F', [(0, 1), (2, 3), (4, 5)])
            np.testing.assert_array_equal(actual, self.data_f[0:2, 2:4, 4:6])

            # Lists/arrays/ranges are explicit contiguous indices.
            for axis in [[0, 1, 2], np.array([0, 1, 2]), range(0, 3)]:
                with self.subTest(type=type(axis).__name__):
                    fh.seek(data_start)
                    actual = nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH, 'F', [axis, None, None])
                    np.testing.assert_array_equal(actual, self.data_f[0:3, :, :])

    def test_empty_region(self):
        data, header, info = nrrd.read(RAW_NRRD_FILE_PATH, region=[(0, -1), None, None])
        self.assertEqual((0, 30, 30), data.shape)
        np.testing.assert_array_equal(header['sizes'], [0, 30, 30])
        self.assertEqual((0, 30, 30), info.shape)

    def test_invalid_regions(self):
        cases = [
            ('Region has 2 axes', [(0, 1), None]),
            ('not contiguous', [(0, 29, 29), None, None]),
            ('step of 2', [slice(None, None, 2), None, None]),
            ('out of bounds', [(0, 99), None, None]),
        ]
        with open(RAW_NRRD_FILE_PATH, 'rb') as fh:
            header = nrrd.read_header(fh)
            for message, region in cases:
                with self.subTest(region=region):
                    with self.assertRaisesRegex(nrrd.NRRDError, message):
                        nrrd.read_data(header, fh, RAW_NRRD_FILE_PATH, 'F', region)

    def test_detached_data_region(self):
        data, header, info = nrrd.read(RAW_NHDR_FILE_PATH, region=[(2, 5), (0, 29), (10, 20)])
        np.testing.assert_array_equal(data, self.data_f[2:6, :, 10:21])
        np.testing.assert_array_equal(header['sizes'], [4, 30, 11])

    def test_byteskip_minus_one_region(self):
        data, _, _ = nrrd.read(RAW_BYTESKIP_NHDR_FILE_PATH, region=[(2, 5), None, (10, 20)])
        np.testing.assert_array_equal(data, self.data_f[2:6, :, 10:21])

    def test_compressed_region_warns_about_full_decode(self):
        for filename, encoding in [(GZ_NRRD_FILE_PATH, 'gzip'), (BZ2_NRRD_FILE_PATH, 'bzip2')]:
            with self.subTest(encoding=encoding):
                with self.assertWarnsRegex(UserWarning, 'cannot be read partially'):
                    data, header, info = nrrd.read(filename, region=[(2, 5), (0, 29), (10, 20)])
                np.testing.assert_array_equal(data, self.data_f[2:6, :, 10:21])
                self.assertTrue(info.full_decode)

    def test_full_compressed_region_does_not_warn(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            data, _, _ = nrrd.read(GZ_NRRD_FILE_PATH, region=[(0, 29), (0, 29), (0, 29)])
        self.assertEqual([], [warning for warning in caught if issubclass(warning.category, UserWarning)])
        np.testing.assert_array_equal(data, self.data_f)

    def test_read_region_convenience(self):
        data, header, info = nrrd.read_region(RAW_NRRD_FILE_PATH, [(2, 5), (0, 29), (10, 20)])
        np.testing.assert_array_equal(data, self.data_f[2:6, :, 10:21])
        np.testing.assert_array_equal(header['sizes'], [4, 30, 11])
        self.assertEqual((30, 30, 30), info.sizes)


class TestCompressedDataIntegrity(unittest.TestCase):
    def test_truncated_compressed_data(self):
        expected = np.arange(8, dtype=np.uint8)
        for encoding, compress in [('gzip', gzip.compress), ('bzip2', bz2.compress)]:
            with self.subTest(encoding=encoding):
                header = {'type': 'uint8', 'dimension': 1, 'sizes': np.array([8]),
                          'encoding': encoding}
                # Removing the stream trailer can leave every decoded voxel intact.
                stream = io.BytesIO(compress(expected.tobytes())[:-1])
                with self.assertRaisesRegex(nrrd.NRRDError, 'end of the compressed stream'):
                    nrrd.read_data(header, stream)

    def test_complete_compressed_data(self):
        expected = np.arange(8, dtype=np.uint8)
        for encoding, compress in [('gzip', gzip.compress), ('bzip2', bz2.compress)]:
            with self.subTest(encoding=encoding):
                header = {'type': 'uint8', 'dimension': 1, 'sizes': np.array([8]),
                          'encoding': encoding}
                stream = io.BytesIO(compress(expected.tobytes()))
                np.testing.assert_array_equal(nrrd.read_data(header, stream), expected)


class TestReadingFunctionsFortran(Abstract.TestReadingFunctions):
    index_order = 'F'


class TestReadingFunctionsC(Abstract.TestReadingFunctions):
    index_order = 'C'


if __name__ == '__main__':
    unittest.main()

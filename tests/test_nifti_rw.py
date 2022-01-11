# Copyright (c) MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import tempfile
import unittest

import nibabel as nib
import numpy as np
from parameterized import parameterized

from monai.data import NibabelWriter
from monai.transforms import LoadImage, Orientation, Spacing
from tests.utils import TEST_NDARRAYS, assert_allclose, make_nifti_image

TESTS = []
for p in TEST_NDARRAYS:
    for q in TEST_NDARRAYS:
        TEST_IMAGE = p(np.arange(24).reshape((2, 4, 3)))
        TEST_AFFINE = q(
            np.array(
                [[-5.3, 0.0, 0.0, 102.01], [0.0, 0.52, 2.17, -7.50], [-0.0, 1.98, -0.26, -23.12], [0.0, 0.0, 0.0, 1.0]]
            )
        )
        TESTS.append(
            [
                TEST_IMAGE,
                TEST_AFFINE,
                dict(reader="NibabelReader", image_only=False, as_closest_canonical=True),
                np.arange(24).reshape((2, 4, 3)),
            ]
        )
        TESTS.append(
            [
                TEST_IMAGE,
                TEST_AFFINE,
                dict(reader="NibabelReader", image_only=True, as_closest_canonical=True),
                np.array(
                    [
                        [[12.0, 15.0, 18.0, 21.0], [13.0, 16.0, 19.0, 22.0], [14.0, 17.0, 20.0, 23.0]],
                        [[0.0, 3.0, 6.0, 9.0], [1.0, 4.0, 7.0, 10.0], [2.0, 5.0, 8.0, 11.0]],
                    ]
                ),
            ]
        )
        TESTS.append(
            [
                TEST_IMAGE,
                TEST_AFFINE,
                dict(reader="NibabelReader", image_only=True, as_closest_canonical=False),
                np.arange(24).reshape((2, 4, 3)),
            ]
        )
        TESTS.append(
            [
                TEST_IMAGE,
                TEST_AFFINE,
                dict(reader="NibabelReader", image_only=False, as_closest_canonical=False),
                np.arange(24).reshape((2, 4, 3)),
            ]
        )
        TESTS.append(
            [
                TEST_IMAGE,
                None,
                dict(reader="NibabelReader", image_only=False, as_closest_canonical=False),
                np.arange(24).reshape((2, 4, 3)),
            ]
        )


class TestNiftiLoadRead(unittest.TestCase):
    @parameterized.expand(TESTS)
    def test_orientation(self, array, affine, reader_param, expected):
        test_image = make_nifti_image(array, affine)

        # read test cases
        loader = LoadImage(**reader_param)
        load_result = loader(test_image)
        if isinstance(load_result, tuple):
            data_array, header = load_result
        else:
            data_array = load_result
            header = None
        if os.path.exists(test_image):
            os.remove(test_image)

        # write test cases
        if header is not None:
            img_obj = NibabelWriter.create_data_obj(data_array, header, channel_dim=None)
            NibabelWriter.write(test_image, img_obj, verbose=True)
        elif affine is not None:
            img_obj = NibabelWriter.create_data_obj(data_array, {"affine": affine}, channel_dim=None)
            NibabelWriter.write(test_image, img_obj, verbose=True)
        saved = nib.load(test_image)
        saved_affine = saved.affine
        saved_data = saved.get_fdata()
        if os.path.exists(test_image):
            os.remove(test_image)

        if affine is not None:
            assert_allclose(saved_affine, affine, type_test=False)
        assert_allclose(saved_data, expected, type_test=False)

    def test_consistency(self):
        np.set_printoptions(suppress=True, precision=3)
        test_image = make_nifti_image(np.arange(64).reshape(1, 8, 8), np.diag([1.5, 1.5, 1.5, 1]))
        data, header = LoadImage(reader="NibabelReader", as_closest_canonical=False)(test_image)
        data, original_affine, new_affine = Spacing([0.8, 0.8, 0.8])(data[None], header["affine"], mode="nearest")
        data, _, new_affine = Orientation("ILP")(data, new_affine)
        if os.path.exists(test_image):
            os.remove(test_image)
        image_obj = NibabelWriter.create_data_obj(
            data_array=data[0],
            metadata={"affine": new_affine, "original_affine": original_affine},
            channel_dim=None,
            kwargs={"mode": "nearest", "padding_mode": "border"},
        )
        NibabelWriter.write(test_image, image_obj, verbose=True)
        saved = nib.load(test_image)
        saved_data = saved.get_fdata()
        np.testing.assert_allclose(saved_data, np.arange(64).reshape(1, 8, 8), atol=1e-7)
        if os.path.exists(test_image):
            os.remove(test_image)
        image_obj = NibabelWriter.create_data_obj(
            data_array=data[0],
            metadata={"affine": new_affine, "original_affine": original_affine, "spatial_shape": (1, 8, 8)},
            channel_dim=None,
            kwargs={"mode": "nearest", "padding_mode": "border"},
        )
        NibabelWriter.write(test_image, image_obj, verbose=True)
        saved = nib.load(test_image)
        saved_data = saved.get_fdata()
        np.testing.assert_allclose(saved_data, np.arange(64).reshape(1, 8, 8), atol=1e-7)
        if os.path.exists(test_image):
            os.remove(test_image)
        # test the case no resample
        image_obj = NibabelWriter.create_data_obj(
            data_array=data[0],
            metadata={"affine": new_affine, "original_affine": original_affine},
            resample=False,
            channel_dim=None,
        )
        NibabelWriter.write(test_image, image_obj, verbose=True)
        saved = nib.load(test_image)
        np.testing.assert_allclose(saved.affine, new_affine)
        if os.path.exists(test_image):
            os.remove(test_image)

    def test_write_2d(self):
        with tempfile.TemporaryDirectory() as out_dir:
            image_name = os.path.join(out_dir, "test.nii.gz")
            for p in TEST_NDARRAYS:
                img = p(np.arange(6).reshape((2, 3)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1]), "original_affine": np.diag([1.4])},
                    channel_dim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[0, 1, 2], [3.0, 4, 5]])
                np.testing.assert_allclose(out.affine, np.diag([1.4, 1, 1, 1]))

                image_name = os.path.join(out_dir, "test1.nii.gz")
                img = np.arange(5).reshape((1, 5))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1, 1, 1, 3, 3]), "original_affine": np.diag([1.4, 2.0, 1, 3, 5])},
                    channel_dim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[0, 2, 4]])
                np.testing.assert_allclose(out.affine, np.diag([1.4, 2, 1, 1]))

    def test_write_3d(self):
        with tempfile.TemporaryDirectory() as out_dir:
            image_name = os.path.join(out_dir, "test.nii.gz")
            for p in TEST_NDARRAYS:
                img = p(np.arange(6).reshape((1, 2, 3)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1]), "original_affine": np.diag([1.4])},
                    channel_dim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[[0, 1, 2], [3, 4, 5]]])
                np.testing.assert_allclose(out.affine, np.diag([1.4, 1, 1, 1]))

                image_name = os.path.join(out_dir, "test1.nii.gz")
                img = p(np.arange(5).reshape((1, 1, 5)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1, 1, 1, 3, 3]), "original_affine": np.diag([1.4, 2.0, 2, 3, 5])},
                    channel_dim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[[0, 2, 4]]])
                np.testing.assert_allclose(out.affine, np.diag([1.4, 2, 2, 1]))

    def test_write_4d(self):
        with tempfile.TemporaryDirectory() as out_dir:
            image_name = os.path.join(out_dir, "test.nii.gz")
            for p in TEST_NDARRAYS:
                img = p(np.arange(6).reshape((1, 1, 3, 2)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1.4, 1]), "original_affine": np.diag([1, 1.4, 1])},
                    channel_dim=-1,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[[[0, 1], [2, 3], [4, 5]]]])
                np.testing.assert_allclose(out.affine, np.diag([1, 1.4, 1, 1]))

                image_name = os.path.join(out_dir, "test1.nii.gz")
                img = p(np.arange(5).reshape((1, 1, 5, 1)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1, 1, 1, 3, 3]), "original_affine": np.diag([1.4, 2.0, 2, 3, 5])},
                    channel_dim=-1,
                    squeeze_end_dims=False,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), [[[[0], [2], [4]]]])
                np.testing.assert_allclose(out.affine, np.diag([1.4, 2, 2, 1]))

    def test_write_5d(self):
        with tempfile.TemporaryDirectory() as out_dir:
            image_name = os.path.join(out_dir, "test.nii.gz")
            for p in TEST_NDARRAYS:
                img = p(np.arange(12).reshape((1, 1, 3, 2, 2)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1]), "original_affine": np.diag([1.4])},
                    channel_dim=-1,
                    squeeze_end_dims=False,
                    spatial_ndim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(
                    out.get_fdata(),
                    np.array([[[[[0.0, 1.0], [2.0, 3.0]], [[4.0, 5.0], [6.0, 7.0]], [[8.0, 9.0], [10.0, 11.0]]]]]),
                )
                np.testing.assert_allclose(out.affine, np.diag([1.4, 1, 1, 1]))

                image_name = os.path.join(out_dir, "test1.nii.gz")
                img = p(np.arange(10).reshape((1, 1, 5, 1, 2)))
                image_obj = NibabelWriter.create_data_obj(
                    data_array=img,
                    metadata={"affine": np.diag([1, 1, 1, 3, 3]), "original_affine": np.diag([1.4, 2.0, 2, 3, 5])},
                    channel_dim=-1,
                    squeeze_end_dims=False,
                    spatial_ndim=None,
                )
                NibabelWriter.write(image_name, image_obj, verbose=True)
                out = nib.load(image_name)
                np.testing.assert_allclose(out.get_fdata(), np.array([[[[[0.0, 1.0]], [[4.0, 5.0]], [[8.0, 9.0]]]]]))
                np.testing.assert_allclose(out.affine, np.diag([1.4, 2, 2, 1]))


if __name__ == "__main__":
    unittest.main()

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

from typing import TYPE_CHECKING, Optional, Sequence, Union

import numpy as np
import torch

from monai.apps.utils import get_logger
from monai.config import DtypeLike, NdarrayOrTensor, PathLike
from monai.data.utils import compute_shape_offset, create_file_basename, ensure_mat44, to_affine_nd
from monai.networks.layers import AffineTransform
from monai.transforms.spatial.array import Resize
from monai.transforms.utils_pytorch_numpy_unification import ascontiguousarray, moveaxis
from monai.utils import (
    GridSampleMode,
    GridSamplePadMode,
    InterpolateMode,
    ensure_tuple_rep,
    look_up_option,
    optional_import,
    require_pkg,
)
from monai.utils.type_conversion import convert_data_type

AFFINE_TOL = 1e-3
DEFAULT_FMT = "%(asctime)s %(levelname)s %(filename)s:%(lineno)d - %(message)s"
logger = get_logger(module_name=__name__, fmt=DEFAULT_FMT)

if TYPE_CHECKING:
    import itk  # type: ignore
    import nibabel as nib
    from nibabel.nifti1 import Nifti1Image
    from PIL import Image as PILImage

    has_itk = has_nib = has_pil = True
else:
    itk, has_itk = optional_import("itk", allow_namespace_pkg=True)
    nib, has_nib = optional_import("nibabel")
    Nifti1Image, _ = optional_import("nibabel.nifti1", name="Nifti1Image")
    PILImage, has_pil = optional_import("PIL.Image")

__all__ = ["ImageWriter", "ITKWriter", "NibabelWriter", "PILWriter", "FolderLayout"]


class FolderLayout:
    """
    A utility class to create organized filenames within ``output_dir``.
    The ``filename`` method could be used to create a filename following the folder structure.

    Example:

    .. code-block:: python

        from monai.data import FolderLayout

        layout = FolderLayout(
            output_dir="/test_run_1/",
            postfix="seg",
            extension=".nii",
            makedirs=False)
        layout.filename(subject="Sub-A", idx="00", modality="T1")
        # return value: "/test_run_1/Sub-A_seg_00_modality-T1.nii"

    The output filename is a string starting with a subject ID, and includes additional information about
    the index and image modality.  This utility class doesn't alter the image data, but provides
    a convenient way to create a filename for each output.
    """

    def __init__(
        self,
        output_dir: PathLike,
        postfix: str = "",
        extension: str = "",
        parent: bool = False,
        makedirs: bool = False,
        data_root_dir: PathLike = "",
    ):
        """
        Args:
            output_dir: output directory.
            postfix: a postfix string for output file name appended to ``subject``.
            extension: output file extension to be appended to the end of an output filename.
            parent: whether to add a level of parent folder to contain each image to the output filename.
            makedirs: whether to create the output parent directories if they do not exist.
            data_root_dir: an optional `PathLike` object to preserve the folder structure of the input `subject`.
                Please see :py:func:`monai.data.utils.create_file_basename` for more details.
        """
        self.output_dir = output_dir
        self.postfix = postfix
        self.ext = extension
        self.parent = parent
        self.makedirs = makedirs
        self.data_root_dir = data_root_dir

    def filename(self, subject="subject", idx=None, **kwargs):
        """
        Create a filename based on the input ``subject`` and ``idx``.

        The output filename is formed as:

            ``output_dir/[subject/]subject[_postfix][_idx][_key-value][ext]``

        Args:
            subject: subject name, used as the primary id of the output filename.
                When a `PathLike` object is provided, the base filename will be used as the subject name,
                the extension name of `subject` will be ignored, in favor of ``extension``
                from this class's constructor.
            idx: additional index name of the image.
            kwargs: additional keyword arguments to be used to form the output filename.
        """
        full_name = create_file_basename(
            postfix=self.postfix,
            input_file_name=subject,
            folder_path=self.output_dir,
            data_root_dir=self.data_root_dir,
            separate_folder=self.parent,
            patch_index=idx,
            makedirs=self.makedirs,
        )
        for k, v in kwargs.items():
            full_name += f"_{k}-{v}"
        if self.ext is not None:
            full_name += f"{self.ext}"
        return full_name


class ImageWriter:
    """
    The class is a collection of utilities to write images to disk.

    Main aspects to be considered are:

        - dimensionality of the data array, arrangements of spatial dimensions and channel/time dimensions
            -  ``convert_to_channel_last()``
        - metadata of current affine and output affine, the data array should be converted accordingly
            - ``get_meta_info()``
            - ``convert_to_target_affine()``
        - data type of the output image
            - as part of ``convert_to_target_affine()``

    Subclasses of this class should implement the backend-specific functions:

        - backend-specific data object
            - ``create_backend_obj()``
        - backend-specific writing function
            - ``write()``

    """

    @classmethod
    def write(cls, filename_or_obj, verbose=True, **kwargs):
        """subclass should implement this method to call the backend-specific writing APIs."""
        if verbose:
            logger.info(f"writing: {filename_or_obj}")

    @classmethod
    def create_backend_obj(cls, data_array, **kwargs):
        """subclass should implement this method to return a backend-specific data representation object.
        This method is used by ``cls.create_data_obj`` and the input ``data_array`` is 'channel-last'.
        """
        return convert_data_type(data_array, np.ndarray)[0]

    @classmethod
    def create_data_obj(
        cls,
        data_array: NdarrayOrTensor,
        metadata: Optional[dict] = None,
        resample: bool = True,
        channel_dim: Optional[int] = 0,
        squeeze_end_dims: bool = True,
        spatial_ndim: Optional[int] = 3,
        output_dtype: DtypeLike = np.float32,
        kwargs=None,
        obj_kwargs=None,
    ):
        """
        Create a image data object based on ``data_array`` and ``metadata``.
        Spatially it supports up to three dimensions (with the resampling step
        supports both 2D and 3D).

        When saving multiple time steps or multiple channels `data_array`, time
        and/or modality axes should be the at the `channel_dim`. For example,
        the shape of a 2D eight-class and channel_dim=0, the segmentation
        probabilities to be saved could be `(8, 64, 64)`; in this case
        ``data_array`` will be converted to `(64, 64, 1, 8)` (the third
        dimension is reserved as a spatial dimension).

        The ``metadata`` could optionally have the following keys:

            - ``'original_affine'``: for data original affine, it will be the
              affine of the output object, defaulting to an identity matrix.
            - ``'affine'``: it should specify the current data affine, defaulting to an identity matrix.
            - ``'spatial_shape'``: for data output spatial shape.

        When ``metadata`` is specified and ``resample=True``, the saver will
        try to resample data from the space defined by `"affine"` to the space
        defined by `"original_affine"`, for more details, please refer to the
        ``convert_to_target_affine`` method.


        Args:
            data_array: input image data.
            metadata: an optional dictionary of meta information.
            resample: whether to convert the data array to its original coordinate system
                based on `"original_affine"` in ``metadata``.
            channel_dim: specifies the axis of the data array that is the channel dimension.
                ``None`` means no channel dimension.
            squeeze_end_dims: if ``True``, any trailing singleton dimensions will be removed (after the channel
                has been moved to the end). So if input is (H,W,D,C) and C==1, then it will be saved as (H,W,D).
                If D is also 1, it will be saved as (H,W). If ``False``, image will always be saved as (H,W,D,C).
            spatial_ndim: modifying the spatial dims if needed, so that output to have at least
                this number of spatial dims. If ``None``, the output will have the same number of
                spatial dimensions as the input.
            output_dtype: data type for the converted ``data_array``. Defaults to ``np.float32``.
            kwargs: additional keyword arguments to pass to :py:func:`ImageWriter.convert_to_target_affine`.
                Current options are: ``mode``, ``padding_mode``, ``align_corners``, ``dtype``.
            obj_kwargs: additional keyword arguments to pass to :py:func:`ImageWriter.create_backend_obj`.
                Current options are implemented by the subclasses.

        Returns: a backend-specific data representation object.
        """
        kwargs = kwargs or {}
        obj_kwargs = obj_kwargs or {}
        # prepare metadata
        original_affine, affine, spatial_shape = cls.get_meta_info(metadata)
        # prepare data array
        data_array = cls.convert_to_channel_last(
            data_array, channel_dim=channel_dim, squeeze_end_dims=squeeze_end_dims, spatial_ndim=spatial_ndim
        )
        # reorient/resample data array according to metadata
        data_array, affine = cls.convert_to_target_affine(
            data_array=data_array,
            affine=affine,
            target_affine=original_affine if resample else None,
            output_spatial_shape=spatial_shape,
            **kwargs,
        )
        return cls.create_backend_obj(
            data_array=data_array, affine=affine, dtype=output_dtype, channel_dim=channel_dim, **obj_kwargs
        )

    @classmethod
    def convert_to_target_affine(
        cls,
        data_array: NdarrayOrTensor,
        affine: Optional[NdarrayOrTensor] = None,
        target_affine: Optional[np.ndarray] = None,
        output_spatial_shape: Union[Sequence[int], np.ndarray, None] = None,
        mode: Union[GridSampleMode, str] = GridSampleMode.BILINEAR,
        padding_mode: Union[GridSamplePadMode, str] = GridSamplePadMode.BORDER,
        align_corners: bool = False,
        dtype: DtypeLike = np.float64,
    ):
        """
        Convert the ``data_array`` into the coordinate system specified by
        ``target_affine``, from the current coordinate definition of ``affine``.

        If the transform between ``affine`` and ``target_affine`` could be
        achieved by simply transposing and flipping ``data_array``, no resampling
        will happen.  Otherwise, this function resamples ``data_array`` using the
        transformation computed from ``affine`` and ``target_affine``.

        This function assumes the NIfTI dimension notations. Spatially it
        supports up to three dimensions, that is, H, HW, HWD for 1D, 2D, 3D
        respectively. When saving multiple time steps or multiple channels,
        time and/or modality axes should be appended after the first three
        dimensions. For example, shape of 2D eight-class segmentation
        probabilities to be saved could be `(64, 64, 1, 8)`. Also, data in
        shape `(64, 64, 8)` or `(64, 64, 8, 1)` will be considered as a
        single-channel 3D image. The ``convert_to_channel_last`` method can be
        used to convert the data to the format described here.

        Note that the shape of the resampled ``data_array`` may subject to some
        rounding errors. For example, resampling a 20x20 pixel image from pixel
        size (1.5, 1.5)-mm to (3.0, 3.0)-mm space will return a 10x10-pixel
        image. However, resampling a 20x20-pixel image from pixel size (2.0,
        2.0)-mm to (3.0, 3.0)-mm space will output a 14x14-pixel image, where
        the image shape is rounded from 13.333x13.333 pixels. In this case
        ``output_spatial_shape`` could be specified so that this function
        writes image data to a designated shape.

        Args:
            data_array: input data array to be converted.
            affine: the current affine of ``data_array``. Defaults to identity
            target_affine: the designated affine of ``data_array``.
                The actual output affine might be different from this value due to precision changes.
            output_spatial_shape: spatial shape of the output image.
                This option is used when resampling is needed.
            mode: available options are {``"bilinear"``, ``"nearest"``, ``"bicubic"``}.
                This option is used when resampling is needed.
                Interpolation mode to calculate output values. Defaults to ``"bilinear"``.
                See also: https://pytorch.org/docs/stable/nn.functional.html#grid-sample
            padding_mode: available options are {``"zeros"``, ``"border"``, ``"reflection"``}.
                This option is used when resampling is needed.
                Padding mode for outside grid values. Defaults to ``"border"``.
                See also: https://pytorch.org/docs/stable/nn.functional.html#grid-sample
            align_corners: boolean option of ``grid_sample`` to handle the corner convention.
                See also: https://pytorch.org/docs/stable/nn.functional.html#grid-sample
            dtype: data type for resampling computation. Defaults to
                ``np.float64`` for best precision. If ``None``, use the data type of input data.
        """
        data: np.ndarray
        data, *_ = convert_data_type(data_array, np.ndarray)  # type: ignore

        sr = min(data.ndim, 3)
        if affine is None:
            affine = np.eye(4, dtype=np.float64)
        affine = to_affine_nd(sr, affine)  # type: ignore
        target_affine = to_affine_nd(sr, target_affine) if target_affine is not None else affine

        if np.allclose(affine, target_affine, atol=AFFINE_TOL):
            # no affine changes, return (data, affine)
            return data, ensure_mat44(target_affine)

        # resolve orientation
        start_ornt = nib.orientations.io_orientation(affine)
        target_ornt = nib.orientations.io_orientation(target_affine)
        ornt_transform = nib.orientations.ornt_transform(start_ornt, target_ornt)
        data_shape = data.shape
        data = nib.orientations.apply_orientation(data, ornt_transform)
        _affine = affine @ nib.orientations.inv_ornt_aff(ornt_transform, data_shape)
        if np.allclose(_affine, target_affine, atol=AFFINE_TOL):
            return data, ensure_mat44(_affine)

        # need resampling
        dtype = dtype or data.dtype  # type: ignore
        if output_spatial_shape is None:
            output_spatial_shape, _ = compute_shape_offset(data.shape, _affine, target_affine)
        output_spatial_shape_ = list(output_spatial_shape) if output_spatial_shape is not None else []
        sp_dims = min(data.ndim, 3)
        output_spatial_shape_ += [1] * (sp_dims - len(output_spatial_shape_))
        output_spatial_shape_ = output_spatial_shape_[:sp_dims]
        original_channels = data.shape[3:]
        if original_channels:  # multi channel, resampling each channel
            data_np: np.ndarray = data.reshape(list(data.shape[:3]) + [-1])  # type: ignore
            data_np = np.moveaxis(data_np, -1, 0)  # channel first for pytorch
        else:  # single channel image, need to expand to have a channel
            data_np = data[None]
        affine_xform = AffineTransform(
            normalized=False, mode=mode, padding_mode=padding_mode, align_corners=align_corners, reverse_indexing=True
        )
        data_torch = affine_xform(
            torch.as_tensor(np.ascontiguousarray(data_np, dtype=dtype)).unsqueeze(0),
            torch.as_tensor(np.ascontiguousarray(np.linalg.inv(_affine) @ target_affine, dtype=dtype)),
            spatial_size=output_spatial_shape_,
        )
        data_np = data_torch[0].detach().cpu().numpy()
        if original_channels:
            data_np = np.moveaxis(data_np, 0, -1)  # channel last
            data_np = data_np.reshape(list(data_np.shape[:3]) + list(original_channels))
        else:
            data_np = data_np[0]
        return data_np, ensure_mat44(target_affine)

    @classmethod
    def convert_to_channel_last(
        cls,
        data: NdarrayOrTensor,
        channel_dim: Optional[int] = 0,
        squeeze_end_dims: bool = True,
        spatial_ndim: Optional[int] = 3,
        contiguous: bool = False,
    ):
        """
        Rearrange the data array axes to make the `channel_dim`-th dim the last
        dimension and ensure there are three spatial dimensions. If
        ``channel_dim`` is ``None``, a new axis will be appended to the last
        dimension.

        When ``squeeze_end_dims`` is ``True``, a postprocessing step will be
        applied to remove any trailing singleton dimensions.

        Args:
            data: input data to be converted to "channel-last" format.
            channel_dim: specifies the axis of the data array that is the channel dimension.
                ``None`` indicates no channel dimension.
            squeeze_end_dims: if ``True``, any trailing singleton dimensions will be removed (after the channel
                has been moved to the end). So if input is `(H,W,D,C)` and C==1, then it will be saved as `(H,W,D)`.
                If D is also 1, it will be saved as `(H,W)`. If ``False``, image will always be saved as `(H,W,D,C)`.
            spatial_ndim: modifying the spatial dims if needed, so that output to have at least
                this number of spatial dims. If ``None``, the output will have the same number of
                spatial dimensions as the input.
            contiguous: if ``True``, the output will be contiguous.
        """
        # change data to "channel last" format
        if channel_dim is not None:
            data = moveaxis(data, channel_dim, -1)
        else:  # adds a channel dimension
            data = data[..., None]
        # To ensure at least three spatial dims
        if spatial_ndim:
            while len(data.shape) < spatial_ndim + 1:  # assuming the data has spatial + channel dims
                data = data[..., None, :]
            while len(data.shape) > spatial_ndim + 1:
                data = data[..., 0, :]

        # if desired, remove trailing singleton dimensions
        while squeeze_end_dims and data.shape[-1] == 1:
            data = np.squeeze(data, -1)
        if contiguous:
            data = ascontiguousarray(data)
        return data

    @classmethod
    def get_meta_info(cls, metadata: Optional[dict] = None):
        """
        Extracts relevant meta information from the metadata object (using ``.get``).

        This method returns the following fields (the default value is ``None``):

            - ``'original_affine'``: for data original affine (before any image processing),
            - ``'affine'``: for the current data affine (representing the current coordinate information),
            - ``'spatial_shape'``: for data original spatial shape.
        """
        if not metadata:
            default_dict = {"original_affine": None, "affine": None, "spatial_shape": None}
            metadata = default_dict
        original_affine = metadata.get("original_affine")
        affine = metadata.get("affine")
        spatial_shape = metadata.get("spatial_shape")
        return original_affine, affine, spatial_shape


@require_pkg(pkg_name="itk")
class ITKWriter(ImageWriter):
    """
    Write data and metadata into files on disk using ITK-python.
    """

    @classmethod
    def create_backend_obj(cls, data_array, channel_dim, affine, dtype, **_kwargs):
        """create an ITK object from ``data_array``.  This method assumes a 'channel-last' ``data_array``."""

        data_array = super().create_backend_obj(data_array)
        _is_vec = channel_dim is not None
        if _is_vec:
            data_array = np.moveaxis(data_array, -1, 0)  # from channel last to channel first
        data_array = data_array.T.astype(dtype, copy=True, order="C")
        itk_obj = itk.GetImageFromArray(data_array, is_vector=_is_vec)

        d = len(itk.size(itk_obj))
        # convert affine to LPS
        affine = ITKWriter.ras_to_lps(to_affine_nd(d, affine))
        spacing = np.sqrt(np.sum(np.square(affine[:d, :d]), 0))
        spacing[spacing == 0] = 1.0
        _direction: np.ndarray = np.diag(1 / spacing)
        _direction = affine[:d, :d] @ _direction
        itk_obj.SetSpacing(spacing.tolist())
        itk_obj.SetOrigin(affine[:d, -1].tolist())
        itk_obj.SetDirection(itk.GetMatrixFromArray(_direction))
        return itk_obj

    @classmethod
    def write(cls, filename_or_obj, data_obj, verbose=False, **kwargs):
        super().write(filename_or_obj, verbose=verbose)
        itk.imwrite(data_obj, filename_or_obj, **kwargs)

    @staticmethod
    def ras_to_lps(affine: NdarrayOrTensor):
        """
        Convert the ``affine`` from `RAS` to `LPS` by flipping the first two spatial dimensions.
        (This could also be used to convert from `LPS` to `RAS`.)

        Args:
            affine: a 2D affine matrix.
        """
        sr = max(affine.shape[0] - 1, 1)  # spatial rank is at least 1
        flip_d = [[-1, 1], [-1, -1, 1], [-1, -1, 1, 1]]
        flip_diag = flip_d[min(sr - 1, 2)] + [1] * (sr - 3)
        if isinstance(affine, torch.Tensor):
            return torch.diag(torch.as_tensor(flip_diag).to(affine)) @ affine
        return np.diag(flip_diag).astype(affine.dtype) @ affine


@require_pkg(pkg_name="nibabel")
class NibabelWriter(ImageWriter):
    """
    Write data and metadata into files on disk using Nibabel.
    """

    @classmethod
    def create_backend_obj(cls, data_array, affine, dtype=None, channel_dim=None, **kwargs):
        """create a Nifti1Image object from ``data_array``. This method assumes a 'channel-last' ``data_array``."""
        data_array = super().create_backend_obj(data_array)
        if dtype is not None:
            data_array = data_array.astype(dtype, copy=False)
        return Nifti1Image(data_array, affine, **kwargs)

    @classmethod
    def write(cls, filename_or_obj, data_obj, verbose=False, **_kwargs):
        super().write(filename_or_obj, verbose=verbose)
        nib.save(data_obj, filename_or_obj)


@require_pkg(pkg_name="PIL")
class PILWriter(ImageWriter):
    """
    Write image data into files on disk using pillow.
    """

    @classmethod
    def create_data_obj(
        cls,
        data_array: NdarrayOrTensor,
        metadata: Optional[dict] = None,
        resample: bool = True,
        channel_dim: Optional[int] = 0,
        squeeze_end_dims: bool = True,
        spatial_ndim: Optional[int] = 2,
        output_dtype: DtypeLike = np.uint8,
        kwargs=None,
        obj_kwargs=None,
    ):
        """
        Args:
            kwargs: current options include ``scale: Optional[int]``  and  ``mode: Union[InterpolateMode, str]``.
            obj_kwargs: current options include ``reverse_indexing``, ``image_mode``.
        """
        kwargs = kwargs or {}
        obj_kwargs = obj_kwargs or {}
        # prepare data array
        spatial_shape = cls.get_meta_info(metadata)
        data_array = cls.convert_to_channel_last(
            data_array, channel_dim=channel_dim, squeeze_end_dims=squeeze_end_dims, spatial_ndim=spatial_ndim
        )
        data_array = cls.resample_and_clip(
            data_array=data_array, output_spatial_shape=spatial_shape if resample else None, **kwargs
        )
        return cls.create_backend_obj(data_array=data_array, channel_dim=channel_dim, dtype=output_dtype, **obj_kwargs)

    @classmethod
    def get_meta_info(cls, metadata: Optional[dict] = None):
        if not metadata:
            return None
        return metadata.get("spatial_shape")

    @classmethod
    def resample_and_clip(
        cls,
        data_array: NdarrayOrTensor,
        output_spatial_shape: Optional[Sequence[int]] = None,
        mode: Union[InterpolateMode, str] = InterpolateMode.BICUBIC,
        scale: Optional[int] = 255,
    ):
        """
        This method assumes the 'channel-last' format
        """

        data: np.ndarray
        data, *_ = convert_data_type(data_array, np.ndarray)  # type: ignore
        if output_spatial_shape is not None:
            if len(data.shape) == 3 and data.shape[2] == 1:
                data = data.squeeze(2)  # PIL Image can't save image with 1 channel
            output_spatial_shape_ = ensure_tuple_rep(output_spatial_shape, 2)
            mode = look_up_option(mode, InterpolateMode)
            align_corners = None if mode in (InterpolateMode.NEAREST, InterpolateMode.AREA) else False
            xform = Resize(spatial_size=output_spatial_shape_, mode=mode, align_corners=align_corners)
            _min, _max = np.min(data), np.max(data)
            if len(data.shape) == 3:
                data = np.moveaxis(data, -1, 0)  # to channel first
                data = xform(data)  # type: ignore
                data = np.moveaxis(data, 0, -1)
            else:  # (H, W)
                data = np.expand_dims(data, 0)  # make a channel
                data = xform(data)[0]  # type: ignore
            if mode != InterpolateMode.NEAREST:
                data = np.clip(data, _min, _max)  # type: ignore

        if not scale:
            return data
        # scale the data to be in an integer range
        data = np.clip(data, 0.0, 1.0)  # type: ignore # png writer only can scale data in range [0, 1]
        if scale == np.iinfo(np.uint8).max:
            return (scale * data).astype(np.uint8, copy=False)
        if scale == np.iinfo(np.uint16).max:
            return (scale * data).astype(np.uint16, copy=False)
        raise ValueError(f"Unsupported scale: {scale}, available options are [255, 65535]")

    @classmethod
    def create_backend_obj(cls, data_array, dtype=None, reverse_indexing=True, image_mode=None, **_kwargs):
        data_array = super().create_backend_obj(data_array)
        if dtype is not None:
            data_array = data_array.astype(dtype, copy=False)
        if reverse_indexing:
            data_array = np.moveaxis(data_array, 0, 1)
        return PILImage.fromarray(data_array, mode=image_mode)

    @classmethod
    def write(cls, filename_or_obj, data_obj, format=None, verbose=False, **kwargs):
        super().write(filename_or_obj, verbose=verbose)
        data_obj.save(filename_or_obj, format=format, **kwargs)

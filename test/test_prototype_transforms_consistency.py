import enum
import inspect
import random
import re
from collections import defaultdict
from importlib.machinery import SourceFileLoader
from pathlib import Path

import numpy as np
import PIL.Image
import pytest

import torch
from prototype_common_utils import (
    ArgsKwargs,
    assert_close,
    assert_equal,
    make_bounding_box,
    make_detection_mask,
    make_image,
    make_images,
    make_label,
    make_segmentation_mask,
)
from torchvision import transforms as legacy_transforms
from torchvision._utils import sequence_to_str
from torchvision.prototype import features, transforms as prototype_transforms
from torchvision.prototype.transforms import functional as prototype_F
from torchvision.prototype.transforms._utils import query_spatial_size
from torchvision.prototype.transforms.functional import to_image_pil
from torchvision.transforms import functional as legacy_F

DEFAULT_MAKE_IMAGES_KWARGS = dict(color_spaces=[features.ColorSpace.RGB], extra_dims=[(4,)])


class ConsistencyConfig:
    def __init__(
        self,
        prototype_cls,
        legacy_cls,
        # If no args_kwargs is passed, only the signature will be checked
        args_kwargs=(),
        make_images_kwargs=None,
        supports_pil=True,
        removed_params=(),
        closeness_kwargs=None,
    ):
        self.prototype_cls = prototype_cls
        self.legacy_cls = legacy_cls
        self.args_kwargs = args_kwargs
        self.make_images_kwargs = make_images_kwargs or DEFAULT_MAKE_IMAGES_KWARGS
        self.supports_pil = supports_pil
        self.removed_params = removed_params
        self.closeness_kwargs = closeness_kwargs or dict(rtol=0, atol=0)


# These are here since both the prototype and legacy transform need to be constructed with the same random parameters
LINEAR_TRANSFORMATION_MEAN = torch.rand(36)
LINEAR_TRANSFORMATION_MATRIX = torch.rand([LINEAR_TRANSFORMATION_MEAN.numel()] * 2)

CONSISTENCY_CONFIGS = [
    ConsistencyConfig(
        prototype_transforms.Normalize,
        legacy_transforms.Normalize,
        [
            ArgsKwargs(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
        ],
        supports_pil=False,
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, dtypes=[torch.float]),
    ),
    ConsistencyConfig(
        prototype_transforms.Resize,
        legacy_transforms.Resize,
        [
            ArgsKwargs(32),
            ArgsKwargs((32, 29)),
            ArgsKwargs((31, 28), interpolation=prototype_transforms.InterpolationMode.NEAREST),
            ArgsKwargs((33, 26), interpolation=prototype_transforms.InterpolationMode.BICUBIC),
            # FIXME: these are currently failing, since the new transform only supports the enum. The int input is
            #  already deprecated and scheduled to be removed in 0.15. Should we support ints on the prototype
            #  transform? I guess it depends if we roll out before 0.15 or not.
            # ArgsKwargs((30, 27), interpolation=0),
            # ArgsKwargs((35, 29), interpolation=2),
            # ArgsKwargs((34, 25), interpolation=3),
            ArgsKwargs(31, max_size=32),
            ArgsKwargs(30, max_size=100),
            ArgsKwargs((29, 32), antialias=False),
            ArgsKwargs((28, 31), antialias=True),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.CenterCrop,
        legacy_transforms.CenterCrop,
        [
            ArgsKwargs(18),
            ArgsKwargs((18, 13)),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.FiveCrop,
        legacy_transforms.FiveCrop,
        [
            ArgsKwargs(18),
            ArgsKwargs((18, 13)),
        ],
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, sizes=[(20, 19)]),
    ),
    ConsistencyConfig(
        prototype_transforms.TenCrop,
        legacy_transforms.TenCrop,
        [
            ArgsKwargs(18),
            ArgsKwargs((18, 13)),
            ArgsKwargs(18, vertical_flip=True),
        ],
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, sizes=[(20, 19)]),
    ),
    ConsistencyConfig(
        prototype_transforms.Pad,
        legacy_transforms.Pad,
        [
            ArgsKwargs(3),
            ArgsKwargs([3]),
            ArgsKwargs([2, 3]),
            ArgsKwargs([3, 2, 1, 4]),
            ArgsKwargs(5, fill=1, padding_mode="constant"),
            ArgsKwargs(5, padding_mode="edge"),
            ArgsKwargs(5, padding_mode="reflect"),
            ArgsKwargs(5, padding_mode="symmetric"),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.LinearTransformation,
        legacy_transforms.LinearTransformation,
        [
            ArgsKwargs(LINEAR_TRANSFORMATION_MATRIX, LINEAR_TRANSFORMATION_MEAN),
        ],
        # Make sure that the product of the height, width and number of channels matches the number of elements in
        # `LINEAR_TRANSFORMATION_MEAN`. For example 2 * 6 * 3 == 4 * 3 * 3 == 36.
        make_images_kwargs=dict(
            DEFAULT_MAKE_IMAGES_KWARGS, sizes=[(2, 6), (4, 3)], color_spaces=[features.ColorSpace.RGB]
        ),
        supports_pil=False,
    ),
    ConsistencyConfig(
        prototype_transforms.Grayscale,
        legacy_transforms.Grayscale,
        [
            ArgsKwargs(num_output_channels=1),
            ArgsKwargs(num_output_channels=3),
        ],
        make_images_kwargs=dict(
            DEFAULT_MAKE_IMAGES_KWARGS, color_spaces=[features.ColorSpace.RGB, features.ColorSpace.GRAY]
        ),
    ),
    ConsistencyConfig(
        prototype_transforms.ConvertDtype,
        legacy_transforms.ConvertImageDtype,
        [
            ArgsKwargs(torch.float16),
            ArgsKwargs(torch.bfloat16),
            ArgsKwargs(torch.float32),
            ArgsKwargs(torch.float64),
            ArgsKwargs(torch.uint8),
        ],
        supports_pil=False,
        # Use default tolerances of `torch.testing.assert_close`
        closeness_kwargs=dict(rtol=None, atol=None),
    ),
    ConsistencyConfig(
        prototype_transforms.ToPILImage,
        legacy_transforms.ToPILImage,
        [ArgsKwargs()],
        make_images_kwargs=dict(
            color_spaces=[
                features.ColorSpace.GRAY,
                features.ColorSpace.GRAY_ALPHA,
                features.ColorSpace.RGB,
                features.ColorSpace.RGB_ALPHA,
            ],
            extra_dims=[()],
        ),
        supports_pil=False,
    ),
    ConsistencyConfig(
        prototype_transforms.Lambda,
        legacy_transforms.Lambda,
        [
            ArgsKwargs(lambda image: image / 2),
        ],
        # Technically, this also supports PIL, but it is overkill to write a function here that supports tensor and PIL
        # images given that the transform does nothing but call it anyway.
        supports_pil=False,
    ),
    ConsistencyConfig(
        prototype_transforms.RandomHorizontalFlip,
        legacy_transforms.RandomHorizontalFlip,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomVerticalFlip,
        legacy_transforms.RandomVerticalFlip,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomEqualize,
        legacy_transforms.RandomEqualize,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, dtypes=[torch.uint8]),
    ),
    ConsistencyConfig(
        prototype_transforms.RandomInvert,
        legacy_transforms.RandomInvert,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomPosterize,
        legacy_transforms.RandomPosterize,
        [
            ArgsKwargs(p=0, bits=5),
            ArgsKwargs(p=1, bits=1),
            ArgsKwargs(p=1, bits=3),
        ],
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, dtypes=[torch.uint8]),
    ),
    ConsistencyConfig(
        prototype_transforms.RandomSolarize,
        legacy_transforms.RandomSolarize,
        [
            ArgsKwargs(p=0, threshold=0.5),
            ArgsKwargs(p=1, threshold=0.3),
            ArgsKwargs(p=1, threshold=0.99),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomAutocontrast,
        legacy_transforms.RandomAutocontrast,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomAdjustSharpness,
        legacy_transforms.RandomAdjustSharpness,
        [
            ArgsKwargs(p=0, sharpness_factor=0.5),
            ArgsKwargs(p=1, sharpness_factor=0.2),
            ArgsKwargs(p=1, sharpness_factor=0.99),
        ],
        closeness_kwargs={"atol": 1e-6, "rtol": 1e-6},
    ),
    ConsistencyConfig(
        prototype_transforms.RandomGrayscale,
        legacy_transforms.RandomGrayscale,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomResizedCrop,
        legacy_transforms.RandomResizedCrop,
        [
            ArgsKwargs(16),
            ArgsKwargs(17, scale=(0.3, 0.7)),
            ArgsKwargs(25, ratio=(0.5, 1.5)),
            ArgsKwargs((31, 28), interpolation=prototype_transforms.InterpolationMode.NEAREST),
            ArgsKwargs((33, 26), interpolation=prototype_transforms.InterpolationMode.BICUBIC),
            ArgsKwargs((29, 32), antialias=False),
            ArgsKwargs((28, 31), antialias=True),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomErasing,
        legacy_transforms.RandomErasing,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
            ArgsKwargs(p=1, scale=(0.3, 0.7)),
            ArgsKwargs(p=1, ratio=(0.5, 1.5)),
            ArgsKwargs(p=1, value=1),
            ArgsKwargs(p=1, value=(1, 2, 3)),
            ArgsKwargs(p=1, value="random"),
        ],
        supports_pil=False,
    ),
    ConsistencyConfig(
        prototype_transforms.ColorJitter,
        legacy_transforms.ColorJitter,
        [
            ArgsKwargs(),
            ArgsKwargs(brightness=0.1),
            ArgsKwargs(brightness=(0.2, 0.3)),
            ArgsKwargs(contrast=0.4),
            ArgsKwargs(contrast=(0.5, 0.6)),
            ArgsKwargs(saturation=0.7),
            ArgsKwargs(saturation=(0.8, 0.9)),
            ArgsKwargs(hue=0.3),
            ArgsKwargs(hue=(-0.1, 0.2)),
            ArgsKwargs(brightness=0.1, contrast=0.4, saturation=0.5, hue=0.6),
        ],
        closeness_kwargs={"atol": 1e-5, "rtol": 1e-5},
    ),
    *[
        ConsistencyConfig(
            prototype_transforms.ElasticTransform,
            legacy_transforms.ElasticTransform,
            [
                ArgsKwargs(),
                ArgsKwargs(alpha=20.0),
                ArgsKwargs(alpha=(15.3, 27.2)),
                ArgsKwargs(sigma=3.0),
                ArgsKwargs(sigma=(2.5, 3.9)),
                ArgsKwargs(interpolation=prototype_transforms.InterpolationMode.NEAREST),
                ArgsKwargs(interpolation=prototype_transforms.InterpolationMode.BICUBIC),
                ArgsKwargs(fill=1),
            ],
            # ElasticTransform needs larger images to avoid the needed internal padding being larger than the actual image
            make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, sizes=[(163, 163), (72, 333), (313, 95)], dtypes=[dt]),
            # We updated gaussian blur kernel generation with a faster and numerically more stable version
            # This brings float32 accumulation visible in elastic transform -> we need to relax consistency tolerance
            closeness_kwargs=ckw,
        )
        for dt, ckw in [(torch.uint8, {"rtol": 1e-1, "atol": 1}), (torch.float32, {"rtol": 1e-2, "atol": 1e-3})]
    ],
    ConsistencyConfig(
        prototype_transforms.GaussianBlur,
        legacy_transforms.GaussianBlur,
        [
            ArgsKwargs(kernel_size=3),
            ArgsKwargs(kernel_size=(1, 5)),
            ArgsKwargs(kernel_size=3, sigma=0.7),
            ArgsKwargs(kernel_size=5, sigma=(0.3, 1.4)),
        ],
        closeness_kwargs={"rtol": 1e-5, "atol": 1e-5},
    ),
    ConsistencyConfig(
        prototype_transforms.RandomAffine,
        legacy_transforms.RandomAffine,
        [
            ArgsKwargs(degrees=30.0),
            ArgsKwargs(degrees=(-20.0, 10.0)),
            ArgsKwargs(degrees=0.0, translate=(0.4, 0.6)),
            ArgsKwargs(degrees=0.0, scale=(0.3, 0.8)),
            ArgsKwargs(degrees=0.0, shear=13),
            ArgsKwargs(degrees=0.0, shear=(8, 17)),
            ArgsKwargs(degrees=0.0, shear=(4, 5, 4, 13)),
            ArgsKwargs(degrees=(-20.0, 10.0), translate=(0.4, 0.6), scale=(0.3, 0.8), shear=(4, 5, 4, 13)),
            ArgsKwargs(degrees=30.0, interpolation=prototype_transforms.InterpolationMode.NEAREST),
            ArgsKwargs(degrees=30.0, fill=1),
            ArgsKwargs(degrees=30.0, fill=(2, 3, 4)),
            ArgsKwargs(degrees=30.0, center=(0, 0)),
        ],
        removed_params=["fillcolor", "resample"],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomCrop,
        legacy_transforms.RandomCrop,
        [
            ArgsKwargs(12),
            ArgsKwargs((15, 17)),
            ArgsKwargs(11, padding=1),
            ArgsKwargs((8, 13), padding=(2, 3)),
            ArgsKwargs((14, 9), padding=(0, 2, 1, 0)),
            ArgsKwargs(36, pad_if_needed=True),
            ArgsKwargs((7, 8), fill=1),
            ArgsKwargs(5, fill=(1, 2, 3)),
            ArgsKwargs(12),
            ArgsKwargs(15, padding=2, padding_mode="edge"),
            ArgsKwargs(17, padding=(1, 0), padding_mode="reflect"),
            ArgsKwargs(8, padding=(3, 0, 0, 1), padding_mode="symmetric"),
        ],
        make_images_kwargs=dict(DEFAULT_MAKE_IMAGES_KWARGS, sizes=[(26, 26), (18, 33), (29, 22)]),
    ),
    ConsistencyConfig(
        prototype_transforms.RandomPerspective,
        legacy_transforms.RandomPerspective,
        [
            ArgsKwargs(p=0),
            ArgsKwargs(p=1),
            ArgsKwargs(p=1, distortion_scale=0.3),
            ArgsKwargs(p=1, distortion_scale=0.2, interpolation=prototype_transforms.InterpolationMode.NEAREST),
            ArgsKwargs(p=1, distortion_scale=0.1, fill=1),
            ArgsKwargs(p=1, distortion_scale=0.4, fill=(1, 2, 3)),
        ],
    ),
    ConsistencyConfig(
        prototype_transforms.RandomRotation,
        legacy_transforms.RandomRotation,
        [
            ArgsKwargs(degrees=30.0),
            ArgsKwargs(degrees=(-20.0, 10.0)),
            ArgsKwargs(degrees=30.0, interpolation=prototype_transforms.InterpolationMode.BILINEAR),
            ArgsKwargs(degrees=30.0, expand=True),
            ArgsKwargs(degrees=30.0, center=(0, 0)),
            ArgsKwargs(degrees=30.0, fill=1),
            ArgsKwargs(degrees=30.0, fill=(1, 2, 3)),
        ],
        removed_params=["resample"],
    ),
    ConsistencyConfig(
        prototype_transforms.PILToTensor,
        legacy_transforms.PILToTensor,
    ),
    ConsistencyConfig(
        prototype_transforms.ToTensor,
        legacy_transforms.ToTensor,
    ),
    ConsistencyConfig(
        prototype_transforms.Compose,
        legacy_transforms.Compose,
    ),
    ConsistencyConfig(
        prototype_transforms.RandomApply,
        legacy_transforms.RandomApply,
    ),
    ConsistencyConfig(
        prototype_transforms.RandomChoice,
        legacy_transforms.RandomChoice,
    ),
    ConsistencyConfig(
        prototype_transforms.RandomOrder,
        legacy_transforms.RandomOrder,
    ),
    ConsistencyConfig(
        prototype_transforms.AugMix,
        legacy_transforms.AugMix,
    ),
    ConsistencyConfig(
        prototype_transforms.AutoAugment,
        legacy_transforms.AutoAugment,
    ),
    ConsistencyConfig(
        prototype_transforms.RandAugment,
        legacy_transforms.RandAugment,
    ),
    ConsistencyConfig(
        prototype_transforms.TrivialAugmentWide,
        legacy_transforms.TrivialAugmentWide,
    ),
]


def test_automatic_coverage():
    available = {
        name
        for name, obj in legacy_transforms.__dict__.items()
        if not name.startswith("_") and isinstance(obj, type) and not issubclass(obj, enum.Enum)
    }

    checked = {config.legacy_cls.__name__ for config in CONSISTENCY_CONFIGS}

    missing = available - checked
    if missing:
        raise AssertionError(
            f"The prototype transformations {sequence_to_str(sorted(missing), separate_last='and ')} "
            f"are not checked for consistency although a legacy counterpart exists."
        )


@pytest.mark.parametrize("config", CONSISTENCY_CONFIGS, ids=lambda config: config.legacy_cls.__name__)
def test_signature_consistency(config):
    legacy_params = dict(inspect.signature(config.legacy_cls).parameters)
    prototype_params = dict(inspect.signature(config.prototype_cls).parameters)

    for param in config.removed_params:
        legacy_params.pop(param, None)

    missing = legacy_params.keys() - prototype_params.keys()
    if missing:
        raise AssertionError(
            f"The prototype transform does not support the parameters "
            f"{sequence_to_str(sorted(missing), separate_last='and ')}, but the legacy transform does. "
            f"If that is intentional, e.g. pending deprecation, please add the parameters to the `removed_params` on "
            f"the `ConsistencyConfig`."
        )

    extra = prototype_params.keys() - legacy_params.keys()
    extra_without_default = {
        param
        for param in extra
        if prototype_params[param].default is inspect.Parameter.empty
        and prototype_params[param].kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
    }
    if extra_without_default:
        raise AssertionError(
            f"The prototype transform requires the parameters "
            f"{sequence_to_str(sorted(extra_without_default), separate_last='and ')}, but the legacy transform does "
            f"not. Please add a default value."
        )

    legacy_kinds = {name: param.kind for name, param in legacy_params.items()}
    prototype_kinds = {name: prototype_params[name].kind for name in legacy_kinds.keys()}
    assert prototype_kinds == legacy_kinds


def check_call_consistency(
    prototype_transform, legacy_transform, images=None, supports_pil=True, closeness_kwargs=None
):
    if images is None:
        images = make_images(**DEFAULT_MAKE_IMAGES_KWARGS)

    closeness_kwargs = closeness_kwargs or dict()

    for image in images:
        image_repr = f"[{tuple(image.shape)}, {str(image.dtype).rsplit('.')[-1]}]"

        image_tensor = torch.Tensor(image)
        try:
            torch.manual_seed(0)
            output_legacy_tensor = legacy_transform(image_tensor)
        except Exception as exc:
            raise pytest.UsageError(
                f"Transforming a tensor image {image_repr} failed in the legacy transform with the "
                f"error above. This means that you need to specify the parameters passed to `make_images` through the "
                "`make_images_kwargs` of the `ConsistencyConfig`."
            ) from exc

        try:
            torch.manual_seed(0)
            output_prototype_tensor = prototype_transform(image_tensor)
        except Exception as exc:
            raise AssertionError(
                f"Transforming a tensor image with shape {image_repr} failed in the prototype transform with "
                f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                f"`is_simple_tensor` path in `_transform`."
            ) from exc

        assert_close(
            output_prototype_tensor,
            output_legacy_tensor,
            msg=lambda msg: f"Tensor image consistency check failed with: \n\n{msg}",
            **closeness_kwargs,
        )

        try:
            torch.manual_seed(0)
            output_prototype_image = prototype_transform(image)
        except Exception as exc:
            raise AssertionError(
                f"Transforming a feature image with shape {image_repr} failed in the prototype transform with "
                f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                f"`features.Image` path in `_transform`."
            ) from exc

        assert_close(
            output_prototype_image,
            output_prototype_tensor,
            msg=lambda msg: f"Output for feature and tensor images is not equal: \n\n{msg}",
            **closeness_kwargs,
        )

        if image.ndim == 3 and supports_pil:
            image_pil = to_image_pil(image)

            try:
                torch.manual_seed(0)
                output_legacy_pil = legacy_transform(image_pil)
            except Exception as exc:
                raise pytest.UsageError(
                    f"Transforming a PIL image with shape {image_repr} failed in the legacy transform with the "
                    f"error above. If this transform does not support PIL images, set `supports_pil=False` on the "
                    "`ConsistencyConfig`. "
                ) from exc

            try:
                torch.manual_seed(0)
                output_prototype_pil = prototype_transform(image_pil)
            except Exception as exc:
                raise AssertionError(
                    f"Transforming a PIL image with shape {image_repr} failed in the prototype transform with "
                    f"the error above. This means there is a consistency bug either in `_get_params` or in the "
                    f"`PIL.Image.Image` path in `_transform`."
                ) from exc

            assert_close(
                output_prototype_pil,
                output_legacy_pil,
                msg=lambda msg: f"PIL image consistency check failed with: \n\n{msg}",
                **closeness_kwargs,
            )


@pytest.mark.parametrize(
    ("config", "args_kwargs"),
    [
        pytest.param(
            config, args_kwargs, id=f"{config.legacy_cls.__name__}-{idx:0{len(str(len(config.args_kwargs)))}d}"
        )
        for config in CONSISTENCY_CONFIGS
        for idx, args_kwargs in enumerate(config.args_kwargs)
    ],
)
@pytest.mark.filterwarnings("ignore")
def test_call_consistency(config, args_kwargs):
    args, kwargs = args_kwargs

    try:
        legacy_transform = config.legacy_cls(*args, **kwargs)
    except Exception as exc:
        raise pytest.UsageError(
            f"Initializing the legacy transform failed with the error above. "
            f"Please correct the `ArgsKwargs({args_kwargs})` in the `ConsistencyConfig`."
        ) from exc

    try:
        prototype_transform = config.prototype_cls(*args, **kwargs)
    except Exception as exc:
        raise AssertionError(
            "Initializing the prototype transform failed with the error above. "
            "This means there is a consistency bug in the constructor."
        ) from exc

    check_call_consistency(
        prototype_transform,
        legacy_transform,
        images=make_images(**config.make_images_kwargs),
        supports_pil=config.supports_pil,
        closeness_kwargs=config.closeness_kwargs,
    )


class TestContainerTransforms:
    """
    Since we are testing containers here, we also need some transforms to wrap. Thus, testing a container transform for
    consistency automatically tests the wrapped transforms consistency.

    Instead of complicated mocking or creating custom transforms just for these tests, here we use deterministic ones
    that were already tested for consistency above.
    """

    def test_compose(self):
        prototype_transform = prototype_transforms.Compose(
            [
                prototype_transforms.Resize(256),
                prototype_transforms.CenterCrop(224),
            ]
        )
        legacy_transform = legacy_transforms.Compose(
            [
                legacy_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ]
        )

        check_call_consistency(prototype_transform, legacy_transform)

    @pytest.mark.parametrize("p", [0, 0.1, 0.5, 0.9, 1])
    def test_random_apply(self, p):
        prototype_transform = prototype_transforms.RandomApply(
            [
                prototype_transforms.Resize(256),
                prototype_transforms.CenterCrop(224),
            ],
            p=p,
        )
        legacy_transform = legacy_transforms.RandomApply(
            [
                legacy_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ],
            p=p,
        )

        check_call_consistency(prototype_transform, legacy_transform)

    # We can't test other values for `p` since the random parameter generation is different
    @pytest.mark.parametrize("probabilities", [(0, 1), (1, 0)])
    def test_random_choice(self, probabilities):
        prototype_transform = prototype_transforms.RandomChoice(
            [
                prototype_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ],
            probabilities=probabilities,
        )
        legacy_transform = legacy_transforms.RandomChoice(
            [
                legacy_transforms.Resize(256),
                legacy_transforms.CenterCrop(224),
            ],
            p=probabilities,
        )

        check_call_consistency(prototype_transform, legacy_transform)


class TestToTensorTransforms:
    def test_pil_to_tensor(self):
        prototype_transform = prototype_transforms.PILToTensor()
        legacy_transform = legacy_transforms.PILToTensor()

        for image in make_images(extra_dims=[()]):
            image_pil = to_image_pil(image)

            assert_equal(prototype_transform(image_pil), legacy_transform(image_pil))

    def test_to_tensor(self):
        with pytest.warns(UserWarning, match=re.escape("The transform `ToTensor()` is deprecated")):
            prototype_transform = prototype_transforms.ToTensor()
        legacy_transform = legacy_transforms.ToTensor()

        for image in make_images(extra_dims=[()]):
            image_pil = to_image_pil(image)
            image_numpy = np.array(image_pil)

            assert_equal(prototype_transform(image_pil), legacy_transform(image_pil))
            assert_equal(prototype_transform(image_numpy), legacy_transform(image_numpy))


class TestAATransforms:
    @pytest.mark.parametrize(
        "inpt",
        [
            torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8),
            PIL.Image.new("RGB", (256, 256), 123),
            features.Image(torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8)),
        ],
    )
    @pytest.mark.parametrize(
        "interpolation",
        [prototype_transforms.InterpolationMode.NEAREST, prototype_transforms.InterpolationMode.BILINEAR],
    )
    def test_randaug(self, inpt, interpolation, mocker):
        t_ref = legacy_transforms.RandAugment(interpolation=interpolation, num_ops=1)
        t = prototype_transforms.RandAugment(interpolation=interpolation, num_ops=1)

        le = len(t._AUGMENTATION_SPACE)
        keys = list(t._AUGMENTATION_SPACE.keys())
        randint_values = []
        for i in range(le):
            # Stable API, op_index random call
            randint_values.append(i)
            # Stable API, if signed there is another random call
            if t._AUGMENTATION_SPACE[keys[i]][1]:
                randint_values.append(0)
            # New API, _get_random_item
            randint_values.append(i)
        randint_values = iter(randint_values)

        mocker.patch("torch.randint", side_effect=lambda *arg, **kwargs: torch.tensor(next(randint_values)))
        mocker.patch("torch.rand", return_value=1.0)

        for i in range(le):
            expected_output = t_ref(inpt)
            output = t(inpt)

            assert_close(expected_output, output, atol=1, rtol=0.1)

    @pytest.mark.parametrize(
        "inpt",
        [
            torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8),
            PIL.Image.new("RGB", (256, 256), 123),
            features.Image(torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8)),
        ],
    )
    @pytest.mark.parametrize(
        "interpolation",
        [prototype_transforms.InterpolationMode.NEAREST, prototype_transforms.InterpolationMode.BILINEAR],
    )
    def test_trivial_aug(self, inpt, interpolation, mocker):
        t_ref = legacy_transforms.TrivialAugmentWide(interpolation=interpolation)
        t = prototype_transforms.TrivialAugmentWide(interpolation=interpolation)

        le = len(t._AUGMENTATION_SPACE)
        keys = list(t._AUGMENTATION_SPACE.keys())
        randint_values = []
        for i in range(le):
            # Stable API, op_index random call
            randint_values.append(i)
            key = keys[i]
            # Stable API, random magnitude
            aug_op = t._AUGMENTATION_SPACE[key]
            magnitudes = aug_op[0](2, 0, 0)
            if magnitudes is not None:
                randint_values.append(5)
            # Stable API, if signed there is another random call
            if aug_op[1]:
                randint_values.append(0)
            # New API, _get_random_item
            randint_values.append(i)
            # New API, random magnitude
            if magnitudes is not None:
                randint_values.append(5)

        randint_values = iter(randint_values)

        mocker.patch("torch.randint", side_effect=lambda *arg, **kwargs: torch.tensor(next(randint_values)))
        mocker.patch("torch.rand", return_value=1.0)

        for _ in range(le):
            expected_output = t_ref(inpt)
            output = t(inpt)

            assert_close(expected_output, output, atol=1, rtol=0.1)

    @pytest.mark.parametrize(
        "inpt",
        [
            torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8),
            PIL.Image.new("RGB", (256, 256), 123),
            features.Image(torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8)),
        ],
    )
    @pytest.mark.parametrize(
        "interpolation",
        [prototype_transforms.InterpolationMode.NEAREST, prototype_transforms.InterpolationMode.BILINEAR],
    )
    def test_augmix(self, inpt, interpolation, mocker):
        t_ref = legacy_transforms.AugMix(interpolation=interpolation, mixture_width=1, chain_depth=1)
        t_ref._sample_dirichlet = lambda t: t.softmax(dim=-1)
        t = prototype_transforms.AugMix(interpolation=interpolation, mixture_width=1, chain_depth=1)
        t._sample_dirichlet = lambda t: t.softmax(dim=-1)

        le = len(t._AUGMENTATION_SPACE)
        keys = list(t._AUGMENTATION_SPACE.keys())
        randint_values = []
        for i in range(le):
            # Stable API, op_index random call
            randint_values.append(i)
            key = keys[i]
            # Stable API, random magnitude
            aug_op = t._AUGMENTATION_SPACE[key]
            magnitudes = aug_op[0](2, 0, 0)
            if magnitudes is not None:
                randint_values.append(5)
            # Stable API, if signed there is another random call
            if aug_op[1]:
                randint_values.append(0)
            # New API, _get_random_item
            randint_values.append(i)
            # New API, random magnitude
            if magnitudes is not None:
                randint_values.append(5)

        randint_values = iter(randint_values)

        mocker.patch("torch.randint", side_effect=lambda *arg, **kwargs: torch.tensor(next(randint_values)))
        mocker.patch("torch.rand", return_value=1.0)

        expected_output = t_ref(inpt)
        output = t(inpt)

        assert_equal(expected_output, output)

    @pytest.mark.parametrize(
        "inpt",
        [
            torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8),
            PIL.Image.new("RGB", (256, 256), 123),
            features.Image(torch.randint(0, 256, size=(1, 3, 256, 256), dtype=torch.uint8)),
        ],
    )
    @pytest.mark.parametrize(
        "interpolation",
        [prototype_transforms.InterpolationMode.NEAREST, prototype_transforms.InterpolationMode.BILINEAR],
    )
    def test_aa(self, inpt, interpolation):
        aa_policy = legacy_transforms.AutoAugmentPolicy("imagenet")
        t_ref = legacy_transforms.AutoAugment(aa_policy, interpolation=interpolation)
        t = prototype_transforms.AutoAugment(aa_policy, interpolation=interpolation)

        torch.manual_seed(12)
        expected_output = t_ref(inpt)

        torch.manual_seed(12)
        output = t(inpt)

        assert_equal(expected_output, output)


def import_transforms_from_references(reference):
    ref_det_filepath = Path(__file__).parent.parent / "references" / reference / "transforms.py"
    return SourceFileLoader(ref_det_filepath.stem, ref_det_filepath.as_posix()).load_module()


det_transforms = import_transforms_from_references("detection")


class TestRefDetTransforms:
    def make_datapoints(self, with_mask=True):
        size = (600, 800)
        num_objects = 22

        pil_image = to_image_pil(make_image(size=size, color_space=features.ColorSpace.RGB))
        target = {
            "boxes": make_bounding_box(spatial_size=size, format="XYXY", extra_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (pil_image, target)

        tensor_image = torch.Tensor(make_image(size=size, color_space=features.ColorSpace.RGB))
        target = {
            "boxes": make_bounding_box(spatial_size=size, format="XYXY", extra_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (tensor_image, target)

        feature_image = make_image(size=size, color_space=features.ColorSpace.RGB)
        target = {
            "boxes": make_bounding_box(spatial_size=size, format="XYXY", extra_dims=(num_objects,), dtype=torch.float),
            "labels": make_label(extra_dims=(num_objects,), categories=80),
        }
        if with_mask:
            target["masks"] = make_detection_mask(size=size, num_objects=num_objects, dtype=torch.long)

        yield (feature_image, target)

    @pytest.mark.parametrize(
        "t_ref, t, data_kwargs",
        [
            (det_transforms.RandomHorizontalFlip(p=1.0), prototype_transforms.RandomHorizontalFlip(p=1.0), {}),
            (det_transforms.RandomIoUCrop(), prototype_transforms.RandomIoUCrop(), {"with_mask": False}),
            (det_transforms.RandomZoomOut(), prototype_transforms.RandomZoomOut(), {"with_mask": False}),
            (det_transforms.ScaleJitter((1024, 1024)), prototype_transforms.ScaleJitter((1024, 1024)), {}),
            (
                det_transforms.FixedSizeCrop((1024, 1024), fill=0),
                prototype_transforms.FixedSizeCrop((1024, 1024), fill=0),
                {},
            ),
            (
                det_transforms.RandomShortestSize(
                    min_size=(480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800), max_size=1333
                ),
                prototype_transforms.RandomShortestSize(
                    min_size=(480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800), max_size=1333
                ),
                {},
            ),
        ],
    )
    def test_transform(self, t_ref, t, data_kwargs):
        for dp in self.make_datapoints(**data_kwargs):

            # We should use prototype transform first as reference transform performs inplace target update
            torch.manual_seed(12)
            output = t(dp)

            torch.manual_seed(12)
            expected_output = t_ref(*dp)

            assert_equal(expected_output, output)


seg_transforms = import_transforms_from_references("segmentation")


# We need this transform for two reasons:
# 1. transforms.RandomCrop uses a different scheme to pad images and masks of insufficient size than its name
#    counterpart in the detection references. Thus, we cannot use it with `pad_if_needed=True`
# 2. transforms.Pad only supports a fixed padding, but the segmentation datasets don't have a fixed image size.
class PadIfSmaller(prototype_transforms.Transform):
    def __init__(self, size, fill=0):
        super().__init__()
        self.size = size
        self.fill = prototype_transforms._geometry._setup_fill_arg(fill)

    def _get_params(self, sample):
        height, width = query_spatial_size(sample)
        padding = [0, 0, max(self.size - width, 0), max(self.size - height, 0)]
        needs_padding = any(padding)
        return dict(padding=padding, needs_padding=needs_padding)

    def _transform(self, inpt, params):
        if not params["needs_padding"]:
            return inpt

        fill = self.fill[type(inpt)]
        return prototype_F.pad(inpt, padding=params["padding"], fill=fill)


class TestRefSegTransforms:
    def make_datapoints(self, supports_pil=True, image_dtype=torch.uint8):
        size = (256, 460)
        num_categories = 21

        conv_fns = []
        if supports_pil:
            conv_fns.append(to_image_pil)
        conv_fns.extend([torch.Tensor, lambda x: x])

        for conv_fn in conv_fns:
            feature_image = make_image(size=size, color_space=features.ColorSpace.RGB, dtype=image_dtype)
            feature_mask = make_segmentation_mask(size=size, num_categories=num_categories, dtype=torch.uint8)

            dp = (conv_fn(feature_image), feature_mask)
            dp_ref = (
                to_image_pil(feature_image) if supports_pil else torch.Tensor(feature_image),
                to_image_pil(feature_mask),
            )

            yield dp, dp_ref

    def set_seed(self, seed=12):
        torch.manual_seed(seed)
        random.seed(seed)

    def check(self, t, t_ref, data_kwargs=None):
        for dp, dp_ref in self.make_datapoints(**data_kwargs or dict()):

            self.set_seed()
            output = t(dp)

            self.set_seed()
            expected_output = t_ref(*dp_ref)

            assert_equal(output, expected_output)

    @pytest.mark.parametrize(
        ("t_ref", "t", "data_kwargs"),
        [
            (
                seg_transforms.RandomHorizontalFlip(flip_prob=1.0),
                prototype_transforms.RandomHorizontalFlip(p=1.0),
                dict(),
            ),
            (
                seg_transforms.RandomHorizontalFlip(flip_prob=0.0),
                prototype_transforms.RandomHorizontalFlip(p=0.0),
                dict(),
            ),
            (
                seg_transforms.RandomCrop(size=480),
                prototype_transforms.Compose(
                    [
                        PadIfSmaller(size=480, fill=defaultdict(lambda: 0, {features.Mask: 255})),
                        prototype_transforms.RandomCrop(size=480),
                    ]
                ),
                dict(),
            ),
            (
                seg_transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                prototype_transforms.Normalize(mean=(0.485, 0.456, 0.406), std=(0.229, 0.224, 0.225)),
                dict(supports_pil=False, image_dtype=torch.float),
            ),
        ],
    )
    def test_common(self, t_ref, t, data_kwargs):
        self.check(t, t_ref, data_kwargs)

    def check_resize(self, mocker, t_ref, t):
        mock = mocker.patch("torchvision.prototype.transforms._geometry.F.resize")
        mock_ref = mocker.patch("torchvision.transforms.functional.resize")

        for dp, dp_ref in self.make_datapoints():
            mock.reset_mock()
            mock_ref.reset_mock()

            self.set_seed()
            t(dp)
            assert mock.call_count == 2
            assert all(
                actual is expected
                for actual, expected in zip([call_args[0][0] for call_args in mock.call_args_list], dp)
            )

            self.set_seed()
            t_ref(*dp_ref)
            assert mock_ref.call_count == 2
            assert all(
                actual is expected
                for actual, expected in zip([call_args[0][0] for call_args in mock_ref.call_args_list], dp_ref)
            )

            for args_kwargs, args_kwargs_ref in zip(mock.call_args_list, mock_ref.call_args_list):
                assert args_kwargs[0][1] == [args_kwargs_ref[0][1]]

    def test_random_resize_train(self, mocker):
        base_size = 520
        min_size = base_size // 2
        max_size = base_size * 2

        randint = torch.randint

        def patched_randint(a, b, *other_args, **kwargs):
            if kwargs or len(other_args) > 1 or other_args[0] != ():
                return randint(a, b, *other_args, **kwargs)

            return random.randint(a, b)

        # We are patching torch.randint -> random.randint here, because we can't patch the modules that are not imported
        # normally
        t = prototype_transforms.RandomResize(min_size=min_size, max_size=max_size, antialias=True)
        mocker.patch(
            "torchvision.prototype.transforms._geometry.torch.randint",
            new=patched_randint,
        )

        t_ref = seg_transforms.RandomResize(min_size=min_size, max_size=max_size)

        self.check_resize(mocker, t_ref, t)

    def test_random_resize_eval(self, mocker):
        torch.manual_seed(0)
        base_size = 520

        t = prototype_transforms.Resize(size=base_size, antialias=True)

        t_ref = seg_transforms.RandomResize(min_size=base_size, max_size=base_size)

        self.check_resize(mocker, t_ref, t)


@pytest.mark.parametrize(
    ("legacy_dispatcher", "name_only_params"),
    [
        (legacy_F.get_dimensions, {}),
        (legacy_F.get_image_size, {}),
        (legacy_F.get_image_num_channels, {}),
        (legacy_F.to_tensor, {}),
        (legacy_F.pil_to_tensor, {}),
        (legacy_F.convert_image_dtype, {}),
        (legacy_F.to_pil_image, {}),
        (legacy_F.normalize, {}),
        (legacy_F.resize, {}),
        (legacy_F.pad, {"padding", "fill"}),
        (legacy_F.crop, {}),
        (legacy_F.center_crop, {}),
        (legacy_F.resized_crop, {}),
        (legacy_F.hflip, {}),
        (legacy_F.perspective, {"startpoints", "endpoints", "fill"}),
        (legacy_F.vflip, {}),
        (legacy_F.five_crop, {}),
        (legacy_F.ten_crop, {}),
        (legacy_F.adjust_brightness, {}),
        (legacy_F.adjust_contrast, {}),
        (legacy_F.adjust_saturation, {}),
        (legacy_F.adjust_hue, {}),
        (legacy_F.adjust_gamma, {}),
        (legacy_F.rotate, {"center", "fill"}),
        (legacy_F.affine, {"angle", "translate", "center", "fill"}),
        (legacy_F.to_grayscale, {}),
        (legacy_F.rgb_to_grayscale, {}),
        (legacy_F.to_tensor, {}),
        (legacy_F.erase, {}),
        (legacy_F.gaussian_blur, {}),
        (legacy_F.invert, {}),
        (legacy_F.posterize, {}),
        (legacy_F.solarize, {}),
        (legacy_F.adjust_sharpness, {}),
        (legacy_F.autocontrast, {}),
        (legacy_F.equalize, {}),
        (legacy_F.elastic_transform, {"fill"}),
    ],
)
def test_dispatcher_signature_consistency(legacy_dispatcher, name_only_params):
    legacy_signature = inspect.signature(legacy_dispatcher)
    legacy_params = list(legacy_signature.parameters.values())[1:]

    try:
        prototype_dispatcher = getattr(prototype_F, legacy_dispatcher.__name__)
    except AttributeError:
        raise AssertionError(
            f"Legacy dispatcher `F.{legacy_dispatcher.__name__}` has no prototype equivalent"
        ) from None

    prototype_signature = inspect.signature(prototype_dispatcher)
    prototype_params = list(prototype_signature.parameters.values())[1:]

    # Some dispatchers got extra parameters. This makes sure they have a default argument and thus are BC. We don't
    # need to check if parameters were added in the middle rather than at the end, since that will be caught by the
    # regular check below.
    prototype_params, new_prototype_params = (
        prototype_params[: len(legacy_params)],
        prototype_params[len(legacy_params) :],
    )
    for param in new_prototype_params:
        assert param.default is not param.empty

    # Some annotations were changed mostly to supersets of what was there before. Plus, some legacy dispatchers had no
    # annotations. In these cases we simply drop the annotation and default argument from the comparison
    for prototype_param, legacy_param in zip(prototype_params, legacy_params):
        if legacy_param.name in name_only_params:
            prototype_param._annotation = prototype_param._default = inspect.Parameter.empty
            legacy_param._annotation = legacy_param._default = inspect.Parameter.empty
        elif legacy_param.annotation is inspect.Parameter.empty:
            prototype_param._annotation = inspect.Parameter.empty

    assert prototype_params == legacy_params

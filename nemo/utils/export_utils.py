# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from enum import Enum
from typing import Callable, Dict, Optional, Type

import onnx
import torch
import torch.nn as nn

from nemo.utils import logging

try:
    import onnxruntime

    ort_available = True
except (ImportError, ModuleNotFoundError):
    ort_available = False


class ExportFormat(Enum):
    """Which format to use when exporting a Neural Module for deployment"""

    ONNX = (1,)
    TORCHSCRIPT = (2,)


_EXT_DICT = {
    ".pt": ExportFormat.TORCHSCRIPT,
    ".ts": ExportFormat.TORCHSCRIPT,
    ".onnx": ExportFormat.ONNX,
}


class CastToFloat(nn.Module):
    def __init__(self, mod):
        super(CastToFloat, self).__init__()
        self.mod = mod

    def forward(self, x):
        if torch.is_autocast_enabled():
            ret = self.mod.forward(x.to(torch.float)).to(x.dtype)
        else:
            ret = self.mod.forward(x)
        return ret


def get_export_format(filename: str):
    _, ext = os.path.splitext(filename)
    try:
        return _EXT_DICT[ext]
    except KeyError:
        raise ValueError(f"Export file {filename} extension does not correspond to any export format!")


def augment_filename(output: str, prepend: str):
    path, filename = os.path.split(output)
    filename = f"{prepend}-{filename}"
    return os.path.join(path, filename)


def forward_method(self):
    if hasattr(self, "forward_for_export"):
        return self.forward_for_export
    else:
        return self.forward


def wrap_forward_method(self):
    tp = type(self)
    old_forward_method = None
    if hasattr(tp, "forward_for_export"):
        forward_method = tp.forward_for_export
        old_forward_method = tp.forward
        tp.forward = forward_method
    else:
        forward_method = None
    return forward_method, old_forward_method


def parse_input_example(input_example):
    input_list = list(input_example)
    input_dict = {}
    # process possible kwargs
    if isinstance(input_list[-1], dict):
        input_dict = input_list[-1]
        input_list = input_list[:-1]
    return input_list, input_dict


def to_onnxrt_input(input_names, input_list, input_dict):
    odict = {}
    for k, v in input_dict.items():
        odict[k] = v.cpu().numpy()
    for i, input in enumerate(input_list):
        if type(input) in (list, tuple):
            odict[input_names[i]] = tuple([ip.cpu().numpy() for ip in input])
        else:
            odict[input_names[i]] = input.cpu().numpy()
    return odict


def verify_runtime(
    output, input_list, input_dict, input_names, output_names, output_example, check_tolerance=0.01,
):
    # Verify the model can be read, and is valid
    onnx_model = onnx.load(output)
    global ort_available
    if not ort_available:
        logging.warning(f"ONNX generated at {output}, not verified - please install onnxruntime_gpu package.\n")
        onnx.checker.check_model(onnx_model, full_check=True)
        return

    onnx_session_opt = onnxruntime.SessionOptions()
    onnx_session_opt.graph_optimization_level = onnxruntime.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = onnxruntime.InferenceSession(
        onnx_model.SerializeToString(), sess_options=onnx_session_opt, providers=['CUDAExecutionProvider']
    )
    ort_out = sess.run(output_names, to_onnxrt_input(input_names, input_list, input_dict))
    all_good = True

    for i, out in enumerate(ort_out[0]):
        expected = output_example[i]
        if torch.is_tensor(expected):
            tout = torch.from_numpy(out)
            if not torch.allclose(tout, expected.cpu(), rtol=check_tolerance, atol=100 * check_tolerance):
                all_good = False
                logging.info(f"onnxruntime results mismatch! PyTorch(expected):\n{expected}\nONNXruntime:\n{tout}")
    status = "SUCCESS" if all_good else "FAIL"
    logging.info(f"ONNX generated at {output} verified with onnxruntime : " + status)


apex_available = True

try:
    from apex.normalization.fused_layer_norm import FusedLayerNorm

    def replace_FusedLayerNorm(n: nn.Module) -> Optional[nn.BatchNorm2d]:
        """
        Replaces Apex's FusedLayerNorm with nn.LayerNorm. This is required for ONNX export.
        Args:
           n: the FusedLayerNorm pytorch module to replace
        Returns:
           Equivalent LayerNorm module
        """
        if not apex_available or not isinstance(n, FusedLayerNorm):
            return None

        dev = next(n.parameters()).device
        mod = nn.LayerNorm(n.normalized_shape, eps=n.eps, elementwise_affine=n.elementwise_affine,).to(dev)

        n_state = n.state_dict()
        mod.load_state_dict(n_state)
        return mod

    default_Apex_replacements = {"FusedLayerNorm": replace_FusedLayerNorm}

except Exception as e:
    default_Apex_replacements = {}
    apex_available = False


def simple_replace(BaseT: Type[nn.Module], DestT: Type[nn.Module]) -> Callable[[nn.Module], Optional[nn.Module]]:
    """
    Generic function generator to replace BaseT module with DestT. BaseT and DestT should have same atrributes. No weights are copied.
    Args:
        BaseT : module type to replace
        DestT : destination module type
    Returns:
        swap function to replace BaseT module with DestT
    """

    def expansion_fn(mod: nn.Module) -> Optional[nn.Module]:
        if not isinstance(mod, BaseT):
            return None
        args = [getattr(mod, name, None) for name in mod.__constants__]
        out = DestT(*args)
        return out

    return expansion_fn


def wrap_module(BaseT: Type[nn.Module], DestT: Type[nn.Module]) -> Callable[[nn.Module], Optional[nn.Module]]:
    """
    Generic function generator to replace BaseT module with DestT wrapper. 
    Args:
        BaseT : module type to replace
        DestT : destination module type
    Returns:
        swap function to replace BaseT module with DestT
    """

    def expansion_fn(mod: nn.Module) -> Optional[nn.Module]:
        out = DestT(mod)
        return out

    return expansion_fn


def swap_modules(model: nn.Module, mapping: Dict[str, nn.Module]):
    """
    This function swaps nested modules as specified by "dot paths" in mod with a desired replacement. This allows
    for swapping nested modules through arbitrary levels if children

    NOTE: This occurs in place, if you want to preserve model then make sure to copy it first.

    """
    for path, new_mod in mapping.items():
        expanded_path = path.split(".")
        parent_mod = model
        for sub_path in expanded_path[:-1]:
            parent_mod = parent_mod._modules[sub_path]  # noqa
        parent_mod._modules[expanded_path[-1]] = new_mod  # noqa

    return model


def replace_modules(
    model: nn.Module, expansions: Dict[str, Callable[[nn.Module], Optional[nn.Module]]] = None
) -> nn.Module:
    """
    Top-level function to replace modules in model, specified by class name with a desired replacement.
    NOTE: This occurs in place, if you want to preserve model then make sure to copy it first.
    Args:
        model : top level module
        expansions : replacement dictionary: module class name -> replacement function generator
    Returns:
        model, possibly modified in-place
    """
    mapping: Dict[str, nn.Module] = {}
    for name, m in model.named_modules():
        m_type = type(m).__name__
        if m_type in expansions:
            swapped = expansions[m_type](m)
            if swapped:
                mapping[name] = swapped
    if len(mapping) > 0:
        logging.info(f"Swapped {len(mapping)} modules")
    swap_modules(model, mapping)
    return model


default_replacements = {
    "BatchNorm1d": wrap_module(nn.BatchNorm1d, CastToFloat),
    "BatchNorm2d": wrap_module(nn.BatchNorm2d, CastToFloat),
    "LayerNorm": wrap_module(nn.LayerNorm, CastToFloat),
}


def replace_for_export(model: nn.Module) -> nn.Module:
    """
    Top-level function to replace default set of modules in model
    NOTE: This occurs in place, if you want to preserve model then make sure to copy it first.
    Args:
        model : top level module
        replace_1D_2D : include 1D -> 2D replacements
    Returns:
        model, possibly modified in-place
    """
    replace_modules(model, default_Apex_replacements)
    replace_modules(model, default_replacements)

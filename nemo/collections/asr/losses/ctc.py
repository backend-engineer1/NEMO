# ! /usr/bin/python
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

import torch
from torch import nn

from nemo.core.classes import Serialization, Typing, typecheck
from nemo.core.neural_types import LabelsType, LengthsType, LogprobsType, LossType, NeuralType
from nemo.utils.decorators import experimental

__all__ = ['CTCLoss']


@experimental
class CTCLoss(nn.CTCLoss, Serialization, Typing):
    @property
    def input_types(self):
        """Input types definitions for CTCLoss.
        """
        return {
            "log_probs": NeuralType(('B', 'T', 'D'), LogprobsType()),
            "targets": NeuralType(('B', 'T'), LabelsType()),
            "input_lengths": NeuralType(tuple('B'), LengthsType()),
            "target_lengths": NeuralType(tuple('B'), LengthsType()),
        }

    @property
    def output_types(self):
        """Output types definitions for CTCLoss.
        loss:
            NeuralType(None)
        """
        return {"loss": NeuralType(elements_type=LossType())}

    def __init__(self, num_classes, zero_infinity=False, reduction='mean_batch'):
        self._blank = num_classes
        # Don't forget to properly call base constructor
        if reduction == 'mean_batch':
            ctc_reduction = 'none'
            self._apply_batch_mean = True
        elif reduction in ['sum', 'mean', 'none']:
            ctc_reduction = reduction
            self._apply_batch_mean = False
        super().__init__(blank=self._blank, reduction=ctc_reduction, zero_infinity=zero_infinity)

    @typecheck()
    def forward(self, log_probs, targets, input_lengths, target_lengths):
        # override forward implementation
        # custom logic, if necessary
        input_lengths = input_lengths.long()
        target_lengths = target_lengths.long()
        targets = targets.long()
        # here we transpose because we expect [B, T, D] while PyTorch assumes [T, B, D]
        log_probs = log_probs.transpose(1, 0)
        loss = super().forward(
            log_probs=log_probs, targets=targets, input_lengths=input_lengths, target_lengths=target_lengths
        )
        if self._apply_batch_mean:
            loss = torch.mean(loss)
        return loss


# Below is how "custom" loss should work
# @experimental
# class CTCLoss(Loss):
#     """
#     CTCLoss
#     Args:
#         num_classes (int): Number of characters in ASR model's vocab/labels.
#             This count should not include the CTC blank symbol.
#         zero_infinity (bool): Whether to zero infinite losses and the associated gradients.
#             By default, it is False. Infinite losses mainly occur when the inputs are too
#             short to be aligned to the targets.
#     """
#
#     def save_to(self, save_path: str):
#         pass
#
#     @classmethod
#     def restore_from(cls, restore_path: str):
#         pass
#
#     @property
#     def input_types(self):
#         """Input types definitions for CTCLoss.
#         """
#         return {
#             "log_probs": NeuralType(('B', 'T', 'D'), LogprobsType()),
#             "targets": NeuralType(('B', 'T'), LabelsType()),
#             "input_length": NeuralType(tuple('B'), LengthsType()),
#             "target_length": NeuralType(tuple('B'), LengthsType()),
#         }
#
#     @property
#     def output_types(self):
#         """Output types definitions for CTCLoss.
#         loss:
#             NeuralType(None)
#         """
#         return {"loss": NeuralType(elements_type=LossType())}
#
#     def __init__(self, num_classes, zero_infinity=False):
#         super().__init__()
#
#         self._blank = num_classes
#         self._criterion = nn.CTCLoss(blank=self._blank, reduction='none', zero_infinity=zero_infinity)
#
#     @typecheck()
#     def forward(self, log_probs, targets, input_length, target_length):
#         input_length = input_length.long()
#         target_length = target_length.long()
#         targets = targets.long()
#         loss = self._criterion(log_probs.transpose(1, 0), targets, input_length, target_length)
#         loss = torch.mean(loss)
#         return loss

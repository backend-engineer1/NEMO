# Copyright 2018 The Google AI Language Team Authors and
# The HuggingFace Inc. team.
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

from transformers import AutoModel, PreTrainedModel

from nemo.collections.nlp.modules.common.bert_module import BertModule
from nemo.utils.decorators import experimental

__all__ = ['AutoModelEncoder']


@experimental
class AutoModelEncoder(PreTrainedModel, BertModule):
    """
    Wraps around the Huggingface transformers implementation repository for easy use within NeMo.
    """

    def __init__(self, pretrained_model_name_or_path):
        BertModule.__init__(self)
        lm_model = AutoModel.from_pretrained(pretrained_model_name_or_path)
        PreTrainedModel.__init__(self, config=lm_model.config)
        self.lm_model = lm_model
        self.type = type(lm_model)

    def forward(self, **kwargs):
        unexpected_keys = set(kwargs.keys()) - set(self.lm_model.forward.__code__.co_varnames)

        for key in unexpected_keys:
            del kwargs[key]
        res = self.lm_model.forward(**kwargs)[0]
        return res

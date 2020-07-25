# Copyright 2020 NVIDIA. All Rights Reserved.
# Copyright 2020 The HuggingFace Inc. team.
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
from typing import List, Optional

import torch
import wget
from transformers import TRANSFORMERS_CACHE, cached_path

from nemo.collections.nlp.modules.common.megatron.megatron_bert import MegatronBertEncoder

__all__ = [
    'get_megatron_lm_model',
    'get_megatron_lm_models_list',
    'get_megatron_checkpoint',
    'is_lower_cased_megatron',
]


MEGATRON_CACHE = os.path.join(os.path.dirname(str(TRANSFORMERS_CACHE)), 'megatron')

CONFIGS = {'345m': {"hidden-size": 1024, "num-attention-heads": 16, "num-layers": 24, "max-seq-length": 512}}

MEGATRON_CONFIG_MAP = {
    'megatron-bert-345m-uncased': {
        'config': CONFIGS['345m'],
        'checkpoint': 'https://api.ngc.nvidia.com/v2/models/nvidia/megatron_bert_345m/versions/v0.0/files/release/mp_rank_00/model_optim_rng.pt',
        'vocab': 'https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-vocab.txt',
        'do_lower_case': True,
    },
    'megatron-bert-345m-cased': {
        'config': CONFIGS['345m'],
        'checkpoint': 'https://api.ngc.nvidia.com/v2/models/nvidia/megatron_bert_345m/versions/v0.1_cased/files/release/mp_rank_00/model_optim_rng.pt',
        'vocab': 'https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-vocab.txt',
        'do_lower_case': False,
    },
    'megatron-bert-uncased': {
        'config': None,
        'checkpoint': None,
        'vocab': 'https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-vocab.txt',
        'do_lower_case': True,
    },
    'megatron-bert-cased': {
        'config': None,
        'vocab': 'https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-vocab.txt',
        'do_lower_case': False,
    },
}


def get_megatron_lm_model(pretrained_model_name: str, config_file: Optional[str] = None):
    '''
    Returns the dict of special tokens associated with the model.
    Args:
        pretrained_mode_name ('str'): name of the pretrained model from the hugging face list,
            for example: bert-base-cased
        config_file: path to model configuration file.
    '''

    if pretrained_model_name == 'megatron-bert-cased' or pretrained_model_name == 'megatron-bert-uncased':
        if not (config_file):
            raise ValueError(f'Config file is required for {pretrained_model_name}')

    config = get_megatron_config(pretrained_model_name)
    if config_file:
        with open(config_file) as f:
            config = json.load(f)

    checkpoint_file = get_megatron_checkpoint(pretrained_model_name)

    vocab = get_megatron_vocab_file(pretrained_model_name)

    model = MegatronBertEncoder(
        model_name=pretrained_model_name,
        vocab_file=vocab,
        hidden_size=config['hidden-size'],
        num_attention_heads=config['num-attention-heads'],
        num_layers=config['num-layers'],
        max_seq_length=config['max-seq-length'],
    )

    return model, checkpoint_file


def get_megatron_lm_models_list() -> List[str]:
    '''
    Return the list of support Megatron models
    '''
    return list(MEGATRON_CONFIG_MAP.keys())


def get_megatron_config(pretrained_model_name):
    '''
    Returns model config file
    Args:
        pretrained_model_name (str): pretrained model name
    Returns:
        config (dict): contains model configuration: number of hidden layers, number of attention heads, etc
    '''
    return MEGATRON_CONFIG_MAP[pretrained_model_name]['config']


def get_megatron_vocab_file(pretrained_model_name):
    '''
    Gets vocabulary file from cache or downloads it
    Args:
        pretrained_model_name (str): pretrained model name
    Returns:
        path (str): path to the vocab file 
    '''
    url = MEGATRON_CONFIG_MAP[pretrained_model_name]['vocab']
    path = cached_path(url, cache_dir=MEGATRON_CACHE)
    return path


def get_megatron_checkpoint(pretrained_model_name):
    '''
    Gets checkpoint file from cache or downloads it
    Args:
        pretrained_model_name (str): pretrained model name
    Returns:
        path (str): path to model checkpoint
    '''
    url = MEGATRON_CONFIG_MAP[pretrained_model_name]['checkpoint']
    path = os.path.join(MEGATRON_CACHE, pretrained_model_name)

    if not os.path.exists(path):
        master_device = not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0
        if not os.path.exists(path):
            if master_device:
                wget.download(url, path)
            # wait until the master process downloads the file and writes it to the cache dir
            if torch.distributed.is_initialized():
                torch.distributed.barrier()

    return path


def is_lower_cased_megatron(pretrained_model_name):
    '''
    Returns if the megatron is cased or uncased
    Args:
        pretrained_model_name (str): pretrained model name
    Returns:
        do_lower_cased (bool): whether the model uses lower cased data
    '''
    return MEGATRON_CONFIG_MAP[pretrained_model_name]['do_lower_case']

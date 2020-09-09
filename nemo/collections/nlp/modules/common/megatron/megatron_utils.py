# Copyright 2020 The HuggingFace Inc. team.
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

import json
import os
from typing import Dict, List, Optional, Tuple

import torch
import wget
from transformers import TRANSFORMERS_CACHE, cached_path

from nemo.collections.nlp.modules.common.megatron.megatron_bert import MegatronBertEncoder
from nemo.utils import logging

__all__ = [
    "get_megatron_lm_model",
    "get_megatron_lm_models_list",
    "get_megatron_checkpoint",
    "is_lower_cased_megatron",
    "get_megatron_tokenizer",
]


MEGATRON_CACHE = os.path.join(os.path.dirname(str(TRANSFORMERS_CACHE)), "megatron")

CONFIGS = {"345m": {"hidden_size": 1024, "num_attention_heads": 16, "num_layers": 24, "max_position_embeddings": 512}}

MEGATRON_CONFIG_MAP = {
    "megatron-bert-345m-uncased": {
        "config": CONFIGS["345m"],
        "checkpoint": "https://api.ngc.nvidia.com/v2/models/nvidia/megatron_bert_345m/versions/v0.0/files/release/mp_rank_00/model_optim_rng.pt",
        "vocab": "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-vocab.txt",
        "do_lower_case": True,
        "tokenizer_name": "bert-large-uncased",
    },
    "megatron-bert-345m-cased": {
        "config": CONFIGS["345m"],
        "checkpoint": "https://api.ngc.nvidia.com/v2/models/nvidia/megatron_bert_345m/versions/v0.1_cased/files/release/mp_rank_00/model_optim_rng.pt",
        "vocab": "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-vocab.txt",
        "do_lower_case": False,
        "tokenizer_name": "bert-large-cased",
    },
    "megatron-bert-uncased": {
        "config": None,
        "checkpoint": None,
        "vocab": "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-uncased-vocab.txt",
        "do_lower_case": True,
        "tokenizer_name": "bert-large-uncased",
    },
    "megatron-bert-cased": {
        "config": None,
        "checkpoint": None,
        "vocab": "https://s3.amazonaws.com/models.huggingface.co/bert/bert-large-cased-vocab.txt",
        "do_lower_case": False,
        "tokenizer_name": "bert-large-cased",
    },
    "biomegatron-bert-345m-uncased": {
        "config": CONFIGS["345m"],
        "checkpoint": "https://api.ngc.nvidia.com/v2/models/nvidia/biomegatron345muncased/versions/0/files/MegatronBERT.pt",
        "vocab": "https://api.ngc.nvidia.com/v2/models/nvidia/biomegatron345muncased/versions/0/files/vocab.txt",
        "do_lower_case": True,
        "tokenizer_name": "bert-large-uncased",
    },
    "biomegatron-bert-345m-cased": {
        "config": CONFIGS["345m"],
        "checkpoint": "https://api.ngc.nvidia.com/v2/models/nvidia/biomegatron345mcased/versions/0/files/MegatronBERT.pt",
        "vocab": "https://api.ngc.nvidia.com/v2/models/nvidia/biomegatron345mcased/versions/0/files/vocab.txt",
        "do_lower_case": False,
        "tokenizer_name": "bert-large-cased",
    },
}


def get_megatron_lm_model(
    pretrained_model_name: str,
    config_dict: Optional[dict] = None,
    config_file: Optional[str] = None,
    checkpoint_file: Optional[str] = None,
) -> Tuple[MegatronBertEncoder, str]:
    """
    Returns MegatronBertEncoder and a default or user specified path to the checkpoint file

    Args:
        pretrained_mode_name: model name from MEGATRON_CONFIG_MAP
            for example: megatron-bert-cased
        config_dict: model configuration parameters
        config_file: path to model configuration file. Takes precedence over config_dict if both supplied.
        checkpoint_file: path to checkpoint file.

    Returns:
        model: MegatronBertEncoder
        checkpoint_file: path to checkpoint file
    """
    config = None
    # get default config and checkpoint
    if config_file:
        with open(config_file) as f:
            configf = json.load(f)
            config = {
                "hidden_size": configf["hidden-size"],
                "num_attention_heads": configf["num-attention-heads"],
                "num_layers": configf["num-layers"],
                "max_position_embeddings": configf["max-seq-length"],
            }
    elif config_dict:
        config = config_dict
    elif pretrained_model_name in get_megatron_lm_models_list():
        config = get_megatron_config(pretrained_model_name)
    else:
        raise ValueError(f"{pretrained_model_name} is not supported")

    if config is None:
        raise ValueError(f"config_file or config_dict is required for {pretrained_model_name}")

    if not checkpoint_file:
        checkpoint_file = get_megatron_checkpoint(pretrained_model_name)

    vocab = get_megatron_vocab_file(pretrained_model_name)

    model = MegatronBertEncoder(config=config, vocab_file=vocab)
    return model, checkpoint_file


def get_megatron_lm_models_list() -> List[str]:
    """
    Returns the list of supported Megatron-LM models
    """
    return list(MEGATRON_CONFIG_MAP.keys())


def get_megatron_config(pretrained_model_name: str) -> Dict[str, int]:
    """
    Returns Megatron-LM model config file

    Args:
        pretrained_model_name (str): pretrained model name

    Returns:
        config (dict): contains model configuration: number of hidden layers, number of attention heads, etc
    """
    return MEGATRON_CONFIG_MAP[pretrained_model_name]["config"]


def get_megatron_vocab_file(pretrained_model_name: str) -> str:
    """
    Gets vocabulary file from cache or downloads it

    Args:
        pretrained_model_name: pretrained model name

    Returns:
        path: path to the vocab file
    """
    url = MEGATRON_CONFIG_MAP[pretrained_model_name]["vocab"]
    path = cached_path(url, cache_dir=MEGATRON_CACHE)

    # try downloading it with wget
    if path is None:
        path = os.path.join(MEGATRON_CACHE, pretrained_model_name + "_vocab")
        path = _download(path, url)
    return path


def get_megatron_checkpoint(pretrained_model_name: str) -> str:
    """
    Gets checkpoint file from cache or downloads it
    Args:
        pretrained_model_name: pretrained model name
    Returns:
        path: path to model checkpoint
    """
    url = MEGATRON_CONFIG_MAP[pretrained_model_name]["checkpoint"]
    path = os.path.join(MEGATRON_CACHE, pretrained_model_name)
    return _download(path, url)


def _download(path: str, url: str):
    """
    Gets a file from cache or downloads it

    Args:
        path: path to the file in cache
        url: url to the file
    Returns:
        path: path to the file in cache
    """
    if url is None:
        return None

    if not os.path.exists(path):
        master_device = not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0
        if not os.path.exists(path):
            if master_device:
                os.makedirs(MEGATRON_CACHE, exist_ok=True)
                logging.info(f"Downloading from {url}")
                wget.download(url, path)
            # wait until the master process downloads the file and writes it to the cache dir
            if torch.distributed.is_initialized():
                torch.distributed.barrier()

    return path


def is_lower_cased_megatron(pretrained_model_name):
    """
    Returns if the megatron is cased or uncased

    Args:
        pretrained_model_name (str): pretrained model name
    Returns:
        do_lower_cased (bool): whether the model uses lower cased data
    """
    return MEGATRON_CONFIG_MAP[pretrained_model_name]["do_lower_case"]


def get_megatron_tokenizer(pretrained_model_name: str):
    """
    Takes a pretrained_model_name for megatron such as "megatron-bert-cased" and returns the according 
    tokenizer name for tokenizer instantiating.

    Args:
        pretrained_model_name: pretrained_model_name for megatron such as "megatron-bert-cased"
    Returns: 
        tokenizer name for tokenizer instantiating
    """
    return MEGATRON_CONFIG_MAP[pretrained_model_name]["tokenizer_name"]

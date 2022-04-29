# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

import torch

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.core.classes import Dataset
from nemo.utils import logging


class SequenceToSequenceDataset(Dataset):
    """Sequence to Sequence Dataset in memory."""

    def __init__(
        self,
        src_file_name: str,
        tgt_file_name: str,
        tokenizer: TokenizerSpec,
        max_src_seq_length: int,
        max_tgt_seq_length: int,
    ):
        self.src_file_name = src_file_name
        self.tgt_file_name = tgt_file_name
        self.tokenizer = tokenizer
        self.max_src_seq_length = max_src_seq_length
        self.max_tgt_seq_length = max_tgt_seq_length
        if not os.path.exists(self.src_file_name):
            raise FileNotFoundError(f"Source file {self.src_file_name} not found")
        if not os.path.exists(self.tgt_file_name):
            raise FileNotFoundError(f"Source file {self.src_file_name} not found")
        assert self.max_src_seq_length > 0
        assert self.max_tgt_seq_length > 0
        self._get_examples()

    def __len__(self):
        return len(self.examples)

    def __getitem__(self, idx):
        example = self.examples[idx]
        text_enc = example['src']
        text_dec = example['tgt'][:-1]
        labels = example['tgt'][1:]
        return {'text_enc': text_enc, 'text_dec': text_dec, 'labels': labels}

    def _get_examples(self):
        self.examples = []
        with open(self.src_file_name, encoding='utf8') as f_src, open(self.tgt_file_name, encoding='utf8') as f_tgt:
            for i, (src, tgt) in enumerate(zip(f_src, f_tgt)):
                if i % 10000 == 0 and i != 0:
                    logging.info(f"Read {i} lines from {self.src_file_name} & {self.tgt_file_name}")
                src = [self.tokenizer.bos_id] + self.tokenizer.text_to_ids(src.strip()) + [self.tokenizer.eos_id]
                tgt = [self.tokenizer.bos_id] + self.tokenizer.text_to_ids(tgt.strip()) + [self.tokenizer.eos_id]
                if len(src) <= self.max_src_seq_length and len(tgt) < self.max_tgt_seq_length:
                    self.examples.append({'src': src, 'tgt': tgt})

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]

        max_dec_input_length = max([len(item) for item in dec_input]) if dec_input else 0
        max_enc_query_length = max([len(item) for item in enc_query]) if enc_query else 0
        max_label_length = max([len(item) for item in labels]) if labels else 0

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tokenizer.pad_id).long()

        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'loss_mask': loss_mask,
            'enc_mask': enc_mask,
            'dec_mask': dec_mask,
        }

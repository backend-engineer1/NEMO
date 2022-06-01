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

import numpy as np
import torch

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.collections.nlp.data.language_modeling.megatron.dataset_utils import get_indexed_dataset_
from nemo.collections.nlp.data.language_modeling.text_memmap_dataset import TextMemMapDataset
from nemo.core.classes import Dataset
from nemo.utils import logging

__all__ = ['SequenceToSequenceDataset', 'TextMemmapSequenceToSequenceDataset']


class SequenceToSequenceDataset(Dataset):
    """Sequence to Sequence Dataset in memory."""

    def __init__(
        self,
        src_file_name: str,
        tgt_file_name: str,
        src_tokenizer: TokenizerSpec,
        tgt_tokenizer: TokenizerSpec,
        max_src_seq_length: int,
        max_tgt_seq_length: int,
    ):
        super().__init__()
        self.src_file_name = src_file_name
        self.tgt_file_name = tgt_file_name
        self.src_tokenizer = src_tokenizer
        self.tgt_tokenizer = tgt_tokenizer
        self.max_src_seq_length = max_src_seq_length
        self.max_tgt_seq_length = max_tgt_seq_length
        assert self.max_src_seq_length > 0
        assert self.max_tgt_seq_length > 0
        self._check_files_exist()
        self._get_examples()

    def _check_files_exist(self):
        if not os.path.exists(self.src_file_name):
            raise FileNotFoundError(f"Source file {self.src_file_name} not found")
        if not os.path.exists(self.tgt_file_name):
            raise FileNotFoundError(f"Source file {self.src_file_name} not found")

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
                src = (
                    [self.src_tokenizer.bos_id]
                    + self.src_tokenizer.text_to_ids(src.strip())
                    + [self.src_tokenizer.eos_id]
                )
                tgt = (
                    [self.tgt_tokenizer.bos_id]
                    + self.tgt_tokenizer.text_to_ids(tgt.strip())
                    + [self.tgt_tokenizer.eos_id]
                )
                if len(src) <= self.max_src_seq_length and len(tgt) < self.max_tgt_seq_length:
                    self.examples.append({'src': src, 'tgt': tgt})

    def collate_fn(self, batch):
        enc_query = [item['text_enc'] for item in batch]
        dec_input = [item['text_dec'] for item in batch]
        labels = [item['labels'] for item in batch]

        if isinstance(enc_query[0], np.ndarray):
            enc_query = [x.tolist() for x in enc_query]

        if isinstance(dec_input[0], np.ndarray):
            dec_input = [x.tolist() for x in dec_input]

        if isinstance(labels[0], np.ndarray):
            labels = [x.tolist() for x in labels]

        max_dec_input_length = max([len(item) for item in dec_input]) if dec_input else 0
        max_enc_query_length = max([len(item) for item in enc_query]) if enc_query else 0
        max_label_length = max([len(item) for item in labels]) if labels else 0

        loss_mask = [([1] * (len(item))) + ([0] * (max_label_length - len(item))) for item in labels]
        enc_query = [item + [self.src_tokenizer.pad_id] * (max_enc_query_length - len(item)) for item in enc_query]
        dec_input = [item + [self.tgt_tokenizer.pad_id] * (max_dec_input_length - len(item)) for item in dec_input]
        labels = [item + [self.tgt_tokenizer.pad_id] * (max_label_length - len(item)) for item in labels]

        enc_query = torch.LongTensor(enc_query)
        dec_input = torch.LongTensor(dec_input)
        labels = torch.LongTensor(labels)
        loss_mask = torch.LongTensor(loss_mask)

        enc_mask = (enc_query != self.src_tokenizer.pad_id).long()
        dec_mask = (dec_input != self.tgt_tokenizer.pad_id).long()

        return {
            'text_enc': enc_query,
            'text_dec': dec_input,
            'labels': labels,
            'loss_mask': loss_mask,
            'enc_mask': enc_mask,
            'dec_mask': dec_mask,
        }


class TextMemmapSequenceToSequenceDataset(SequenceToSequenceDataset):
    """Sequence to Sequence Dataset in memory."""

    def __init__(
        self,
        src_file_name: str,
        tgt_file_name: str,
        src_tokenizer: TokenizerSpec,
        tgt_tokenizer: TokenizerSpec,
        max_src_seq_length: int,
        max_tgt_seq_length: int,
    ):
        super().__init__(
            src_file_name=src_file_name,
            tgt_file_name=tgt_file_name,
            src_tokenizer=src_tokenizer,
            tgt_tokenizer=tgt_tokenizer,
            max_src_seq_length=max_src_seq_length,
            max_tgt_seq_length=max_tgt_seq_length,
        )

    def __len__(self):
        return len(self.src_dataset)

    def __getitem__(self, idx):
        src = self.src_dataset[idx]
        if len(src) > self.max_src_seq_length - 2:
            src = src[: self.max_src_seq_length - 2]

        src = [self.src_tokenizer.bos_id] + src + [self.src_tokenizer.eos_id]
        tgt = self.tgt_dataset[idx]

        if len(tgt) > self.max_tgt_seq_length - 1:
            tgt = tgt[: self.max_tgt_seq_length - 1]

        text_enc = src
        text_dec = [self.tgt_tokenizer.bos_id] + tgt
        labels = tgt + [self.tgt_tokenizer.eos_id]
        return {'text_enc': text_enc, 'text_dec': text_dec, 'labels': labels}

    def _get_examples(self):
        self.src_dataset = TextMemMapDataset(dataset_paths=[self.src_file_name], tokenizer=self.src_tokenizer)
        self.tgt_dataset = TextMemMapDataset(dataset_paths=[self.tgt_file_name], tokenizer=self.tgt_tokenizer)
        assert len(self.src_dataset) == len(self.tgt_dataset), "src and tgt has different number of lines"


class BinarizedMemmapSequenceToSequenceDataset(SequenceToSequenceDataset):
    """Sequence to Sequence Dataset based on Megatron Dataset Utils."""

    def __init__(
        self,
        src_dataset_prefix,
        tgt_dataset_prefix,
        src_tokenizer,
        tgt_tokenizer,
        max_src_seq_length,
        max_tgt_seq_length,
        start_index=0,
        end_index=None,
        data_impl='mmap',
        skip_warmup=True,
        seed=1337,
    ):
        self.src_dataset_prefix = src_dataset_prefix
        self.tgt_dataset_prefix = tgt_dataset_prefix
        self.src_tokenizer = src_tokenizer
        self.tgt_tokenizer = tgt_tokenizer
        self.data_impl = data_impl
        self.skip_warmup = skip_warmup
        self.start_index = start_index
        self.end_index = end_index
        self.max_src_seq_length = max_src_seq_length
        self.max_tgt_seq_length = max_tgt_seq_length
        self.seed = seed
        super().__init__(
            src_file_name=src_dataset_prefix,
            tgt_file_name=tgt_dataset_prefix,
            src_tokenizer=src_tokenizer,
            tgt_tokenizer=tgt_tokenizer,
            max_src_seq_length=max_src_seq_length,
            max_tgt_seq_length=max_tgt_seq_length,
        )
        self._dataset_length = lambda dataset: dataset.sizes.shape[0]
        if not end_index:
            self.end_index = self._dataset_length(self.src_indexed_dataset) - 1
        self._print_stats('Source Dataset', self.start_index, self.end_index)
        self._print_stats('Target Dataset', self.start_index, self.end_index)

    def __len__(self):
        self.end_index - self.start_index

    def _check_files_exist(self):
        if not os.path.exists(self.src_dataset_prefix + ".bin") or not os.path.exists(
            self.src_dataset_prefix + ".idx"
        ):
            raise FileNotFoundError(f"{self.src_dataset_prefix}.bin or {self.src_dataset_prefix}.idx not found")
        if not os.path.exists(self.tgt_dataset_prefix + ".bin") or not os.path.exists(
            self.tgt_dataset_prefix + ".idx"
        ):
            raise FileNotFoundError(f"{self.tgt_dataset_prefix}.bin or {self.tgt_dataset_prefix}.idx not found")

    def _get_examples(self):
        self.src_indexed_dataset = self._get_indexed_dataset(self.src_dataset_prefix, self.data_impl, self.skip_warmup)
        self.tgt_indexed_dataset = self._get_indexed_dataset(self.tgt_dataset_prefix, self.data_impl, self.skip_warmup)
        assert len(self.src_indexed_dataset) == len(self.tgt_indexed_dataset)

    def _get_indexed_dataset(self, data_prefix, data_impl, skip_warmup):
        indexed_dataset = get_indexed_dataset_(data_prefix, data_impl, skip_warmup)
        return indexed_dataset

    def _print_stats(self, name, start, end):
        logging.info('    {}:'.format(name))
        logging.info('     sentence indices in [{}, {}) total of {} ' 'sentences'.format(start, end, end - start))

    def __len__(self):
        return self.end_index - self.start_index

    def __getitem__(self, idx):
        if isinstance(idx, np.int64):
            idx = idx.item()

        local_idx = idx + self.start_index
        assert local_idx < self.end_index

        src = self.src_indexed_dataset[local_idx]
        if len(src) > self.max_src_seq_length - 2:
            src = src[: self.max_src_seq_length - 2]
        text_enc = np.concatenate([[self.src_tokenizer.bos_id], src, [self.src_tokenizer.eos_id]])

        tgt = self.tgt_indexed_dataset[local_idx]
        if len(tgt) > self.max_tgt_seq_length - 2:
            tgt = tgt[: self.max_tgt_seq_length - 2]

        text_dec = np.concatenate([[self.tgt_tokenizer.bos_id], tgt])
        labels = np.concatenate([tgt, [self.tgt_tokenizer.eos_id]])

        return {'text_enc': text_enc, 'text_dec': text_dec, 'labels': labels}

# Copyright (c) 2019 NVIDIA Corporation
"""
If you want to add your own data layer, you should put its name in
__all__ so that it can be imported with 'from text_data_layers import *'
"""
__all__ = ['TextDataLayer',
           'BertSentenceClassificationDataLayer',
           'BertJointIntentSlotDataLayer',
           'BertJointIntentSlotInferDataLayer',
           'LanguageModelingDataLayer',
           'BertTokenClassificationDataLayer',
           'BertPretrainingDataLayer',
           'TranslationDataLayer',
           'GlueDataLayerClassification',
           'GlueDataLayerRegression']

# from abc import abstractmethod
import sys

import torch
from torch.utils import data as pt_data

import nemo
from nemo.backends.pytorch.nm import DataLayerNM
from nemo.core.neural_types import *

from .datasets import *


class TextDataLayer(DataLayerNM):
    """
    Generic Text Data Layer NM which wraps PyTorch's dataset

    Args:
        dataset_type: type of dataset used for this datalayer
        dataset_params (dict): all the params for the dataset
    """

    def __init__(self, dataset_type, dataset_params, **kwargs):
        super().__init__(**kwargs)
        if isinstance(dataset_type, str):
            dataset_type = getattr(sys.modules[__name__], dataset_type)
        self._dataset = dataset_type(**dataset_params)

    def __len__(self):
        return len(self._dataset)

    @property
    def dataset(self):
        return self._dataset

    @property
    def data_iterator(self):
        return None


class BertSentenceClassificationDataLayer(TextDataLayer):
    """
    Creates the data layer to use for the task of sentence classification
    with pretrained model.

    All the data processing is done BertSentenceClassificationDataset.

    Args:
        dataset (BertSentenceClassificationDataset):
                the dataset that needs to be converted to DataLayerNM
    """

    @staticmethod
    def create_ports():
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({
                0: AxisType(BatchTag),
            }),
        }
        return {}, output_ports

    def __init__(self,
                 input_file,
                 tokenizer,
                 max_seq_length,
                 num_samples=-1,
                 shuffle=False,
                 batch_size=64,
                 dataset_type=BertSentenceClassificationDataset,
                 **kwargs):
        kwargs['batch_size'] = batch_size
        dataset_params = {'input_file': input_file,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length,
                          'num_samples': num_samples,
                          'shuffle': shuffle}
        super().__init__(dataset_type, dataset_params, **kwargs)


class BertJointIntentSlotDataLayer(TextDataLayer):
    """
    Creates the data layer to use for the task of joint intent
    and slot classification with pretrained model.

    All the data processing is done in BertJointIntentSlotDataset.

    Args:
        dataset (BertJointIntentSlotDataset):
                the dataset that needs to be converted to DataLayerNM
    """
    @staticmethod
    def create_ports():
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "token_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "intents": NeuralType({
                0: AxisType(BatchTag),
            }),
            "slots": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
        }
        return {}, output_ports

    def __init__(self,
                 input_file,
                 slot_file,
                 pad_label,
                 tokenizer,
                 max_seq_length,
                 num_samples=-1,
                 shuffle=False,
                 batch_size=64,
                 dataset_type=BertJointIntentSlotDataset,
                 **kwargs):
        kwargs['batch_size'] = batch_size
        dataset_params = {'input_file': input_file,
                          'slot_file': slot_file,
                          'pad_label': pad_label,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length,
                          'num_samples': num_samples,
                          'shuffle': shuffle}
        super().__init__(dataset_type, dataset_params, **kwargs)


class BertJointIntentSlotInferDataLayer(TextDataLayer):
    """
    Creates the data layer to use for the task of joint intent
    and slot classification with pretrained model. This is for

    All the data processing is done in BertJointIntentSlotInferDataset.

    Args:
        dataset (BertJointIntentSlotInferDataset):
                the dataset that needs to be converted to DataLayerNM
    """
    @staticmethod
    def create_ports():
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "token_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            })
        }
        return {}, output_ports

    def __init__(self,
                 queries,
                 tokenizer,
                 max_seq_length,
                 batch_size=1,
                 dataset_type=BertJointIntentSlotInferDataset,
                 **kwargs):
        kwargs['batch_size'] = batch_size
        dataset_params = {'queries': queries,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length}
        super().__init__(dataset_type, dataset_params, **kwargs)


class LanguageModelingDataLayer(TextDataLayer):
    """
    Data layer for standard language modeling task.

    Args:
        dataset (str): path to text document with data
        tokenizer (TokenizerSpec): tokenizer
        max_seq_length (int): maximum allowed length of the text segments
        batch_step (int): how many tokens to skip between two successive
            segments of text when constructing batches
    """

    @staticmethod
    def create_ports():
        """
        input_ids: indices of tokens which constitute batches of text segments
        input_mask: bool tensor with 0s in place of tokens to be masked
        labels: indices of tokens which should be predicted from each of the
            corresponding tokens in input_ids; for left-to-right language
            modeling equals to input_ids shifted by 1 to the right
        """
        input_ports = {}
        output_ports = {
            "input_ids":
            NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask":
            NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels":
            NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            })
        }

        return input_ports, output_ports

    def __init__(self,
                 dataset,
                 tokenizer,
                 max_seq_length,
                 batch_step=128,
                 dataset_type=LanguageModelingDataset,
                 **kwargs):
        dataset_params = {'dataset': dataset,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length,
                          'batch_step': batch_step}
        super().__init__(dataset_type, dataset_params, **kwargs)


class BertTokenClassificationDataLayer(TextDataLayer):
    @staticmethod
    def create_ports():
        input_ports = {}
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "seq_ids": NeuralType({0: AxisType(BatchTag)})
        }
        return input_ports, output_ports

    def __init__(self,
                 input_file,
                 tokenizer,
                 max_seq_length,
                 batch_size=64,
                 dataset_type=BertTokenClassificationDataset,
                 **kwargs):
        kwargs['batch_size'] = batch_size
        dataset_params = {'input_file': input_file,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length}
        super().__init__(dataset_type, dataset_params, **kwargs)

    def eval_preds(self, logits, seq_ids, tag_ids):
        return self._dataset.eval_preds(logits, seq_ids, tag_ids)


class BertPretrainingDataLayer(TextDataLayer):
    """
    Data layer for masked language modeling task.

    Args:
        tokenizer (TokenizerSpec): tokenizer
        dataset (str): directory or a single file with dataset documents
        max_seq_length (int): maximum allowed length of the text segments
        mask_probability (float): probability of masking input sequence tokens
        batch_size (int): batch size in segments
        short_seeq_prob (float): Probability of creating sequences which are
            shorter than the maximum length.
            Defualts to 0.1.
    """

    @staticmethod
    def create_ports():
        """
        input_ids: indices of tokens which constitute batches of text segments
        input_type_ids: indices of token types (e.g., sentences A & B in BERT)
        input_mask: bool tensor with 0s in place of tokens to be masked
        output_ids: indices of output tokens which should be predicted
        output_mask: bool tensor with 0s in place of tokens to be excluded
            from loss calculation
        labels: indices of classes to be predicted from [CLS] token of text
            segments (e.g, 0 or 1 in next sentence prediction task)
        """
        input_ports = {}
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "output_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "output_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({0: AxisType(BatchTag)}),
        }

        return input_ports, output_ports

    def __init__(self,
                 tokenizer,
                 dataset,
                 max_seq_length,
                 mask_probability,
                 short_seq_prob=0.1,
                 batch_size=64,
                 **kwargs):
        kwargs['batch_size'] = batch_size
        dataset_params = {'tokenizer': tokenizer,
                          'dataset': dataset,
                          'max_seq_length': max_seq_length,
                          'mask_probability': mask_probability,
                          'short_seq_prob': short_seq_prob}
        super().__init__(BertPretrainingDataset, dataset_params, **kwargs)


class TranslationDataLayer(TextDataLayer):
    """
    Data layer for neural machine translation from source (src) language to
    target (tgt) language.

    Args:
        tokenizer_src (TokenizerSpec): source language tokenizer
        tokenizer_tgt (TokenizerSpec): target language tokenizer
        dataset_src (str): path to source data
        dataset_tgt (str): path to target data
        tokens_in_batch (int): maximum allowed number of tokens in batches,
            batches will be constructed to minimize the use of <pad> tokens
        clean (bool): whether to use parallel data cleaning such as removing
            pairs with big difference in sentences length, removing pairs with
            the same tokens in src and tgt, etc; useful for training data layer
            and should not be used in evaluation data layer
    """

    @staticmethod
    def create_ports():
        """
        src_ids: indices of tokens which correspond to source sentences
        src_mask: bool tensor with 0s in place of source tokens to be masked
        tgt_ids: indices of tokens which correspond to target sentences
        tgt_mask: bool tensor with 0s in place of target tokens to be masked
        labels: indices of tokens which should be predicted from each of the
            corresponding target tokens in tgt_ids; for standard neural
            machine translation equals to tgt_ids shifted by 1 to the right
        sent_ids: indices of the sentences in a batch; important for
            evaluation with external metrics, such as SacreBLEU
        """
        input_ports = {}
        output_ports = {
            "src_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "src_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "tgt_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "tgt_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "sent_ids": NeuralType({
                0: AxisType(BatchTag)
            })
        }

        return input_ports, output_ports

    def __init__(self,
                 tokenizer_src,
                 tokenizer_tgt,
                 dataset_src,
                 dataset_tgt,
                 tokens_in_batch=1024,
                 clean=False,
                 dataset_type=TranslationDataset,
                 **kwargs):
        dataset_params = {'tokenizer_src': tokenizer_src,
                          'tokenizer_tgt': tokenizer_tgt,
                          'dataset_src': dataset_src,
                          'dataset_tgt': dataset_tgt,
                          'tokens_in_batch': tokens_in_batch,
                          'clean': clean}
        super().__init__(dataset_type, dataset_params, **kwargs)

        if self._placement == nemo.core.DeviceType.AllGpu:
            sampler = pt_data.distributed.DistributedSampler(self._dataset)
        else:
            sampler = None

        self._dataloader = pt_data.DataLoader(dataset=self._dataset,
                                              batch_size=1,
                                              collate_fn=self._collate_fn,
                                              shuffle=sampler is None,
                                              sampler=sampler)

    def _collate_fn(self, x):
        src_ids, src_mask, tgt_ids, tgt_mask, labels, sent_ids = x[0]
        src_ids = torch.Tensor(src_ids).long().to(self._device)
        src_mask = torch.Tensor(src_mask).float().to(self._device)
        tgt_ids = torch.Tensor(tgt_ids).long().to(self._device)
        tgt_mask = torch.Tensor(tgt_mask).float().to(self._device)
        labels = torch.Tensor(labels).long().to(self._device)
        sent_ids = torch.Tensor(sent_ids).long().to(self._device)
        return src_ids, src_mask, tgt_ids, tgt_mask, labels, sent_ids

    @property
    def dataset(self):
        return None

    @property
    def data_iterator(self):
        return self._dataloader


class GlueDataLayerClassification(TextDataLayer):
    """
    Creates the data layer to use for the GLUE classification tasks,
    more details here: https://gluebenchmark.com/tasks

    All the data processing is done in GLUEDataset.

    Args:
        dataset_type (GLUEDataset):
                the dataset that needs to be converted to DataLayerNM
    """

    @staticmethod
    def create_ports():
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({
                0: AxisType(CategoricalTag),
            }),
        }
        return {}, output_ports

    def __init__(self,
                 data_dir,
                 tokenizer,
                 max_seq_length,
                 processor,
                 evaluate=False,
                 token_params={},
                 num_samples=-1,
                 shuffle=False,
                 batch_size=64,
                 dataset_type=GLUEDataset,
                 **kwargs):

        kwargs['batch_size'] = batch_size
        dataset_params = {'data_dir': data_dir,
                          'output_mode': 'classification',
                          'processor': processor,
                          'evaluate': evaluate,
                          'token_params': token_params,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length}

        super().__init__(dataset_type, dataset_params, **kwargs)


class GlueDataLayerRegression(TextDataLayer):
    """
    Creates the data layer to use for the GLUE STS-B regression task,
    more details here: https://gluebenchmark.com/tasks

    All the data processing is done in GLUEDataset.

    Args:
        dataset_type (GLUEDataset):
                the dataset that needs to be converted to DataLayerNM
    """

    @staticmethod
    def create_ports():
        output_ports = {
            "input_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_type_ids": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "input_mask": NeuralType({
                0: AxisType(BatchTag),
                1: AxisType(TimeTag)
            }),
            "labels": NeuralType({
                0: AxisType(RegressionTag),
            }),
        }
        return {}, output_ports

    def __init__(self,
                 data_dir,
                 tokenizer,
                 max_seq_length,
                 processor,
                 evaluate=False,
                 token_params={},
                 num_samples=-1,
                 shuffle=False,
                 batch_size=64,
                 dataset_type=GLUEDataset,
                 **kwargs):

        kwargs['batch_size'] = batch_size
        dataset_params = {'data_dir': data_dir,
                          'output_mode': 'regression',
                          'processor': processor,
                          'evaluate': evaluate,
                          'token_params': token_params,
                          'tokenizer': tokenizer,
                          'max_seq_length': max_seq_length}

        super().__init__(dataset_type, dataset_params, **kwargs)

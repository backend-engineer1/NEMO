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

import itertools
import json
import pickle
import random
from multiprocessing import Value
from pathlib import Path
from typing import Dict, List, Optional, Union

import numpy as np
import torch
import torch.distributed as dist
import torch.utils.data as pt_data
from omegaconf import DictConfig, ListConfig, OmegaConf
from pytorch_lightning import Trainer
from pytorch_lightning.utilities import rank_zero_only
from sacrebleu import corpus_bleu

from nemo.collections.common.losses import NLLLoss, SmoothedCrossEntropyLoss
from nemo.collections.common.metrics import GlobalAverageLossMetric
from nemo.collections.common.parts import transformer_weights_init
from nemo.collections.common.tokenizers.chinese_tokenizers import ChineseProcessor
from nemo.collections.common.tokenizers.en_ja_tokenizers import EnJaProcessor
from nemo.collections.common.tokenizers.moses_tokenizers import MosesProcessor
from nemo.collections.nlp.data import TarredTranslationDataset, TranslationDataset
from nemo.collections.nlp.models.enc_dec_nlp_model import EncDecNLPModel
from nemo.collections.nlp.models.machine_translation.mt_enc_dec_config import MTEncDecModelConfig
from nemo.collections.nlp.modules.common import TokenClassifier
from nemo.collections.nlp.modules.common.lm_utils import get_transformer
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.collections.nlp.modules.common.transformer import BeamSearchSequenceGenerator, TopKSequenceGenerator
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.utils import logging, model_utils

__all__ = ['MTEncDecModel']


class MTEncDecModel(EncDecNLPModel):
    """
    Encoder-decoder machine translation model.
    """

    def __init__(self, cfg: MTEncDecModelConfig, trainer: Trainer = None):
        cfg = model_utils.convert_model_config_to_dict_config(cfg)
        # Get global rank and total number of GPU workers for IterableDataset partitioning, if applicable
        # Global_rank and local_rank is set by LightningModule in Lightning 1.2.0

        self.world_size = 1
        if trainer is not None:
            self.world_size = trainer.num_nodes * trainer.num_gpus

        cfg = model_utils.maybe_update_config_version(cfg)

        self.src_language: str = cfg.get("src_language", None)
        self.tgt_language: str = cfg.get("tgt_language", None)

        # Instantiates tokenizers and register to be saved with NeMo Model archive
        # After this call, ther will be self.encoder_tokenizer and self.decoder_tokenizer
        # Which can convert between tokens and token_ids for SRC and TGT languages correspondingly.
        self.setup_enc_dec_tokenizers(
            encoder_tokenizer_library=cfg.encoder_tokenizer.get('library', 'yttm'),
            encoder_tokenizer_model=cfg.encoder_tokenizer.get('tokenizer_model'),
            encoder_bpe_dropout=cfg.encoder_tokenizer.get('bpe_dropout', 0.0),
            encoder_model_name=cfg.encoder.get('model_name') if hasattr(cfg.encoder, 'model_name') else None,
            decoder_tokenizer_library=cfg.decoder_tokenizer.get('library', 'yttm'),
            decoder_tokenizer_model=cfg.decoder_tokenizer.tokenizer_model,
            decoder_bpe_dropout=cfg.decoder_tokenizer.get('bpe_dropout', 0.0),
            decoder_model_name=cfg.decoder.get('model_name') if hasattr(cfg.decoder, 'model_name') else None,
        )

        # After this call, the model will have  self.source_processor and self.target_processor objects
        self.setup_pre_and_post_processing_utils(source_lang=self.src_language, target_lang=self.tgt_language)

        # TODO: Why is this base constructor call so late in the game?
        super().__init__(cfg=cfg, trainer=trainer)

        # encoder from NeMo, Megatron-LM, or HuggingFace
        encoder_cfg_dict = OmegaConf.to_container(cfg.get('encoder'))
        encoder_cfg_dict['vocab_size'] = self.encoder_vocab_size
        library = encoder_cfg_dict.pop('library', 'nemo')
        model_name = encoder_cfg_dict.pop('model_name', None)
        pretrained = encoder_cfg_dict.pop('pretrained', False)
        self.encoder = get_transformer(
            library=library,
            model_name=model_name,
            pretrained=pretrained,
            config_dict=encoder_cfg_dict,
            encoder=True,
            pre_ln_final_layer_norm=encoder_cfg_dict.get('pre_ln_final_layer_norm', False),
        )

        # decoder from NeMo, Megatron-LM, or HuggingFace
        decoder_cfg_dict = OmegaConf.to_container(cfg.get('decoder'))
        decoder_cfg_dict['vocab_size'] = self.decoder_vocab_size
        library = decoder_cfg_dict.pop('library', 'nemo')
        model_name = decoder_cfg_dict.pop('model_name', None)
        pretrained = decoder_cfg_dict.pop('pretrained', False)
        decoder_cfg_dict['hidden_size'] = self.encoder.hidden_size
        self.decoder = get_transformer(
            library=library,
            model_name=model_name,
            pretrained=pretrained,
            config_dict=decoder_cfg_dict,
            encoder=False,
            pre_ln_final_layer_norm=decoder_cfg_dict.get('pre_ln_final_layer_norm', False),
        )

        self.log_softmax = TokenClassifier(
            hidden_size=self.decoder.hidden_size,
            num_classes=self.decoder_vocab_size,
            activation=cfg.head.activation,
            log_softmax=cfg.head.log_softmax,
            dropout=cfg.head.dropout,
            use_transformer_init=cfg.head.use_transformer_init,
        )

        self.beam_search = BeamSearchSequenceGenerator(
            embedding=self.decoder.embedding,
            decoder=self.decoder.decoder,
            log_softmax=self.log_softmax,
            max_sequence_length=self.decoder.max_sequence_length,
            beam_size=cfg.beam_size,
            bos=self.decoder_tokenizer.bos_id,
            pad=self.decoder_tokenizer.pad_id,
            eos=self.decoder_tokenizer.eos_id,
            len_pen=cfg.len_pen,
            max_delta_length=cfg.max_generation_delta,
        )

        # tie weights of embedding and softmax matrices
        self.log_softmax.mlp.layer0.weight = self.decoder.embedding.token_embedding.weight

        # TODO: encoder and decoder with different hidden size?
        std_init_range = 1 / self.encoder.hidden_size ** 0.5

        # initialize weights if not using pretrained encoder/decoder
        if not self._cfg.encoder.get('pretrained', False):
            self.encoder.apply(lambda module: transformer_weights_init(module, std_init_range))

        if not self._cfg.decoder.get('pretrained', False):
            self.decoder.apply(lambda module: transformer_weights_init(module, std_init_range))

        self.log_softmax.apply(lambda module: transformer_weights_init(module, std_init_range))

        self.loss_fn = SmoothedCrossEntropyLoss(
            pad_id=self.decoder_tokenizer.pad_id, label_smoothing=cfg.label_smoothing
        )
        self.eval_loss_fn = NLLLoss(ignore_index=self.decoder_tokenizer.pad_id)

    def filter_predicted_ids(self, ids):
        ids[ids >= self.decoder_tokenizer.vocab_size] = self.decoder_tokenizer.unk_id
        return ids

    @typecheck()
    def forward(self, src, src_mask, tgt, tgt_mask):
        src_hiddens = self.encoder(input_ids=src, encoder_mask=src_mask)
        tgt_hiddens = self.decoder(
            input_ids=tgt, decoder_mask=tgt_mask, encoder_embeddings=src_hiddens, encoder_mask=src_mask
        )
        log_probs = self.log_softmax(hidden_states=tgt_hiddens)
        return log_probs

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        # forward pass
        for i in range(len(batch)):
            if batch[i].ndim == 3:
                # Dataset returns already batched data and the first dimension of size 1 added by DataLoader
                # is excess.
                batch[i] = batch[i].squeeze(dim=0)
        src_ids, src_mask, tgt_ids, tgt_mask, labels = batch
        log_probs = self(src_ids, src_mask, tgt_ids, tgt_mask)
        train_loss = self.loss_fn(log_probs=log_probs, labels=labels)
        tensorboard_logs = {
            'train_loss': train_loss,
            'lr': self._optimizer.param_groups[0]['lr'],
        }
        return {'loss': train_loss, 'log': tensorboard_logs}

    def eval_step(self, batch, batch_idx, mode, dataloader_idx=0):
        for i in range(len(batch)):
            if batch[i].ndim == 3:
                # Dataset returns already batched data and the first dimension of size 1 added by DataLoader
                # is excess.
                batch[i] = batch[i].squeeze(dim=0)
        src_ids, src_mask, tgt_ids, tgt_mask, labels = batch
        log_probs = self(src_ids, src_mask, tgt_ids, tgt_mask)
        eval_loss = self.eval_loss_fn(log_probs=log_probs, labels=labels)
        # this will run encoder twice -- TODO: potentially fix
        _, translations = self.batch_translate(src=src_ids, src_mask=src_mask)
        if dataloader_idx == 0:
            getattr(self, f'{mode}_loss')(loss=eval_loss, num_measurements=log_probs.shape[0] * log_probs.shape[1])
        else:
            getattr(self, f'{mode}_loss_{dataloader_idx}')(
                loss=eval_loss, num_measurements=log_probs.shape[0] * log_probs.shape[1]
            )
        np_tgt = tgt_ids.detach().cpu().numpy()
        ground_truths = [self.decoder_tokenizer.ids_to_text(tgt) for tgt in np_tgt]
        ground_truths = [self.target_processor.detokenize(tgt.split(' ')) for tgt in ground_truths]
        num_non_pad_tokens = np.not_equal(np_tgt, self.decoder_tokenizer.pad_id).sum().item()
        return {
            'translations': translations,
            'ground_truths': ground_truths,
            'num_non_pad_tokens': num_non_pad_tokens,
        }

    def test_step(self, batch, batch_idx, dataloader_idx=0):
        return self.eval_step(batch, batch_idx, 'test', dataloader_idx)

    @rank_zero_only
    def log_param_stats(self):
        for name, p in self.named_parameters():
            if p.requires_grad:
                self.trainer.logger.experiment.add_histogram(name + '_hist', p, global_step=self.global_step)
                self.trainer.logger.experiment.add_scalars(
                    name,
                    {'mean': p.mean(), 'stddev': p.std(), 'max': p.max(), 'min': p.min()},
                    global_step=self.global_step,
                )

    def validation_step(self, batch, batch_idx, dataloader_idx=0):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        return self.eval_step(batch, batch_idx, 'val', dataloader_idx)

    def eval_epoch_end(self, outputs, mode):
        # if user specifies one validation dataloader, then PTL reverts to giving a list of dictionary instead of a list of list of dictionary
        if isinstance(outputs[0], dict):
            outputs = [outputs]
        for dataloader_idx, output in enumerate(outputs):
            if dataloader_idx == 0:
                eval_loss = getattr(self, f'{mode}_loss').compute()
            else:
                eval_loss = getattr(self, f'{mode}_loss_{dataloader_idx}').compute()

            translations = list(itertools.chain(*[x['translations'] for x in output]))
            ground_truths = list(itertools.chain(*[x['ground_truths'] for x in output]))
            assert len(translations) == len(ground_truths)

            # Gather translations and ground truths from all workers
            tr_and_gt = [None for _ in range(self.world_size)]
            # we also need to drop pairs where ground truth is an empty string
            dist.all_gather_object(
                tr_and_gt, [(t, g) for (t, g) in zip(translations, ground_truths) if g.strip() != '']
            )
            if self.global_rank == 0:
                _translations = []
                _ground_truths = []
                for rank in range(0, self.world_size):
                    _translations += [t for (t, g) in tr_and_gt[rank]]
                    _ground_truths += [g for (t, g) in tr_and_gt[rank]]

                if self.tgt_language in ['ja']:
                    sacre_bleu = corpus_bleu(_translations, [_ground_truths], tokenize="ja-mecab")
                elif self.tgt_language in ['zh']:
                    sacre_bleu = corpus_bleu(_translations, [_ground_truths], tokenize="zh")
                else:
                    sacre_bleu = corpus_bleu(_translations, [_ground_truths], tokenize="13a")

                # because the reduction op later is average (over word_size)
                sb_score = sacre_bleu.score * self.world_size

                dataset_name = "Validation" if mode == 'val' else "Test"
                logging.info(
                    f"Dataset name: {dataset_name}, Dataloader index: {dataloader_idx}, Set size: {len(translations)}"
                )
                logging.info(
                    f"Dataset name: {dataset_name}, Dataloader index: {dataloader_idx}, Val Loss = {eval_loss}"
                )
                logging.info(
                    f"Dataset name: {dataset_name}, Dataloader index: {dataloader_idx}, Sacre BLEU = {sb_score / self.world_size}"
                )
                logging.info(
                    f"Dataset name: {dataset_name}, Dataloader index: {dataloader_idx}, Translation Examples:"
                )
                for i in range(0, 3):
                    ind = random.randint(0, len(translations) - 1)
                    logging.info("    " + '\u0332'.join(f"Example {i}:"))
                    logging.info(f"    Prediction:   {translations[ind]}")
                    logging.info(f"    Ground Truth: {ground_truths[ind]}")
            else:
                sb_score = 0.0

            if dataloader_idx == 0:
                self.log(f"{mode}_loss", eval_loss, sync_dist=True)
                self.log(f"{mode}_sacreBLEU", sb_score, sync_dist=True)
                getattr(self, f'{mode}_loss').reset()
            else:
                self.log(f"{mode}_loss_dl_index_{dataloader_idx}", eval_loss, sync_dist=True)
                self.log(f"{mode}_sacreBLEU_dl_index_{dataloader_idx}", sb_score, sync_dist=True)
                getattr(self, f'{mode}_loss_{dataloader_idx}').reset()

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        :param outputs: list of individual outputs of each validation step.
        """
        self.eval_epoch_end(outputs, 'val')

    def test_epoch_end(self, outputs):
        self.eval_epoch_end(outputs, 'test')

    def setup_enc_dec_tokenizers(
        self,
        encoder_tokenizer_library=None,
        encoder_tokenizer_model=None,
        encoder_bpe_dropout=0.0,
        encoder_model_name=None,
        decoder_tokenizer_library=None,
        decoder_tokenizer_model=None,
        decoder_bpe_dropout=0.0,
        decoder_model_name=None,
    ):

        supported_tokenizers = ['yttm', 'huggingface', 'sentencepiece']
        if (
            encoder_tokenizer_library not in supported_tokenizers
            or decoder_tokenizer_library not in supported_tokenizers
        ):
            raise NotImplementedError(f"Currently we only support tokenizers in {supported_tokenizers}.")

        self.encoder_tokenizer = get_nmt_tokenizer(
            library=encoder_tokenizer_library,
            tokenizer_model=self.register_artifact("encoder_tokenizer.tokenizer_model", encoder_tokenizer_model),
            bpe_dropout=encoder_bpe_dropout,
            model_name=encoder_model_name,
            vocab_file=None,
            special_tokens=None,
            use_fast=False,
        )
        self.decoder_tokenizer = get_nmt_tokenizer(
            library=decoder_tokenizer_library,
            tokenizer_model=self.register_artifact("decoder_tokenizer.tokenizer_model", decoder_tokenizer_model),
            bpe_dropout=decoder_bpe_dropout,
            model_name=decoder_model_name,
            vocab_file=None,
            special_tokens=None,
            use_fast=False,
        )

    def setup_training_data(self, train_data_config: Optional[DictConfig]):
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

    def setup_multiple_validation_data(self, val_data_config: Union[DictConfig, Dict]):
        self.setup_validation_data(self._cfg.get('validation_ds'))

    def setup_multiple_test_data(self, test_data_config: Union[DictConfig, Dict]):
        self.setup_test_data(self._cfg.get('test_ds'))

    def setup_validation_data(self, val_data_config: Optional[DictConfig]):
        self._validation_dl = self._setup_eval_dataloader_from_config(cfg=val_data_config)
        # instantiate Torchmetric for each val dataloader
        if self._validation_dl is not None:
            for dataloader_idx in range(len(self._validation_dl)):
                if dataloader_idx == 0:
                    setattr(
                        self, f'val_loss', GlobalAverageLossMetric(dist_sync_on_step=False, take_avg_loss=True),
                    )
                else:
                    setattr(
                        self,
                        f'val_loss_{dataloader_idx}',
                        GlobalAverageLossMetric(dist_sync_on_step=False, take_avg_loss=True),
                    )

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        self._test_dl = self._setup_eval_dataloader_from_config(cfg=test_data_config)
        # instantiate Torchmetric for each test dataloader
        if self._test_dl is not None:
            for dataloader_idx in range(len(self._test_dl)):
                if dataloader_idx == 0:
                    setattr(
                        self, f'test_loss', GlobalAverageLossMetric(dist_sync_on_step=False, take_avg_loss=True),
                    )
                else:
                    setattr(
                        self,
                        f'test_loss_{dataloader_idx}',
                        GlobalAverageLossMetric(dist_sync_on_step=False, take_avg_loss=True),
                    )

    def _setup_dataloader_from_config(self, cfg: DictConfig):
        if cfg.get("load_from_cached_dataset", False):
            logging.info('Loading from cached dataset %s' % (cfg.src_file_name))
            if cfg.src_file_name != cfg.tgt_file_name:
                raise ValueError("src must be equal to target for cached dataset")
            dataset = pickle.load(open(cfg.src_file_name, 'rb'))
            dataset.reverse_lang_direction = cfg.get("reverse_lang_direction", False)
        elif cfg.get("use_tarred_dataset", False):
            if cfg.get("metadata_file") is None:
                raise FileNotFoundError("Trying to use tarred data set but could not find metadata path in config.")
            else:
                metadata_file = cfg.get('metadata_file')
                with open(metadata_file) as metadata_reader:
                    metadata = json.load(metadata_reader)
                if cfg.get('tar_files') is None:
                    tar_files = metadata.get('tar_files')
                    if tar_files is not None:
                        logging.info(f'Loading from tarred dataset {tar_files}')
                    else:
                        raise FileNotFoundError("Could not find tarred dataset in config or metadata.")
                else:
                    tar_files = cfg.get('tar_files')
                    if metadata.get('tar_files') is not None:
                        logging.info(
                            f'Tar file paths found in both cfg and metadata using one in cfg by default - {tar_files}'
                        )

            dataset = TarredTranslationDataset(
                text_tar_filepaths=tar_files,
                metadata_path=metadata_file,
                encoder_tokenizer=self.encoder_tokenizer,
                decoder_tokenizer=self.decoder_tokenizer,
                shuffle_n=cfg.get("tar_shuffle_n", 100),
                shard_strategy=cfg.get("shard_strategy", "scatter"),
                global_rank=self.global_rank,
                world_size=self.world_size,
                reverse_lang_direction=cfg.get("reverse_lang_direction", False),
            )
            return torch.utils.data.DataLoader(
                dataset=dataset,
                batch_size=1,
                num_workers=cfg.get("num_workers", 2),
                pin_memory=cfg.get("pin_memory", False),
                drop_last=cfg.get("drop_last", False),
            )
        else:
            dataset = TranslationDataset(
                dataset_src=str(Path(cfg.src_file_name).expanduser()),
                dataset_tgt=str(Path(cfg.tgt_file_name).expanduser()),
                tokens_in_batch=cfg.tokens_in_batch,
                clean=cfg.get("clean", False),
                max_seq_length=cfg.get("max_seq_length", 512),
                min_seq_length=cfg.get("min_seq_length", 1),
                max_seq_length_diff=cfg.get("max_seq_length_diff", 512),
                max_seq_length_ratio=cfg.get("max_seq_length_ratio", 512),
                cache_ids=cfg.get("cache_ids", False),
                cache_data_per_node=cfg.get("cache_data_per_node", False),
                use_cache=cfg.get("use_cache", False),
                reverse_lang_direction=cfg.get("reverse_lang_direction", False),
            )
            dataset.batchify(self.encoder_tokenizer, self.decoder_tokenizer)
        if cfg.shuffle:
            sampler = pt_data.RandomSampler(dataset)
        else:
            sampler = pt_data.SequentialSampler(dataset)
        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=1,
            sampler=sampler,
            num_workers=cfg.get("num_workers", 2),
            pin_memory=cfg.get("pin_memory", False),
            drop_last=cfg.get("drop_last", False),
        )

    def replace_beam_with_sampling(self, topk=500):
        self.beam_search = TopKSequenceGenerator(
            embedding=self.decoder.embedding,
            decoder=self.decoder.decoder,
            log_softmax=self.log_softmax,
            max_sequence_length=self.beam_search.max_seq_length,
            beam_size=topk,
            bos=self.decoder_tokenizer.bos_id,
            pad=self.decoder_tokenizer.pad_id,
            eos=self.decoder_tokenizer.eos_id,
        )

    def _setup_eval_dataloader_from_config(self, cfg: DictConfig):
        src_file_name = cfg.get('src_file_name')
        tgt_file_name = cfg.get('tgt_file_name')

        if src_file_name is None or tgt_file_name is None:
            raise ValueError(
                'Validation dataloader needs both cfg.src_file_name and cfg.tgt_file_name to not be None.'
            )
        else:
            # convert src_file_name and tgt_file_name to list of strings
            if isinstance(src_file_name, str):
                src_file_list = [src_file_name]
            elif isinstance(src_file_name, ListConfig):
                src_file_list = src_file_name
            else:
                raise ValueError("cfg.src_file_name must be string or list of strings")
            if isinstance(tgt_file_name, str):
                tgt_file_list = [tgt_file_name]
            elif isinstance(tgt_file_name, ListConfig):
                tgt_file_list = tgt_file_name
            else:
                raise ValueError("cfg.tgt_file_name must be string or list of strings")
        if len(src_file_list) != len(tgt_file_list):
            raise ValueError('The same number of filepaths must be passed in for source and target validation.')

        dataloaders = []
        for src_file, tgt_file in zip(src_file_list, tgt_file_list):
            dataset = TranslationDataset(
                dataset_src=str(Path(src_file).expanduser()),
                dataset_tgt=str(Path(tgt_file).expanduser()),
                tokens_in_batch=cfg.tokens_in_batch,
                clean=cfg.get("clean", False),
                max_seq_length=cfg.get("max_seq_length", 512),
                min_seq_length=cfg.get("min_seq_length", 1),
                max_seq_length_diff=cfg.get("max_seq_length_diff", 512),
                max_seq_length_ratio=cfg.get("max_seq_length_ratio", 512),
                cache_ids=cfg.get("cache_ids", False),
                cache_data_per_node=cfg.get("cache_data_per_node", False),
                use_cache=cfg.get("use_cache", False),
                reverse_lang_direction=cfg.get("reverse_lang_direction", False),
            )
            dataset.batchify(self.encoder_tokenizer, self.decoder_tokenizer)

            if cfg.shuffle:
                sampler = pt_data.RandomSampler(dataset)
            else:
                sampler = pt_data.SequentialSampler(dataset)

            dataloader = torch.utils.data.DataLoader(
                dataset=dataset,
                batch_size=1,
                sampler=sampler,
                num_workers=cfg.get("num_workers", 2),
                pin_memory=cfg.get("pin_memory", False),
                drop_last=cfg.get("drop_last", False),
            )
            dataloaders.append(dataloader)

        return dataloaders

    def setup_pre_and_post_processing_utils(self, source_lang, target_lang):
        """
        Creates source and target processor objects for input and output pre/post-processing.
        """
        self.source_processor, self.target_processor = None, None
        if (source_lang == 'en' and target_lang == 'ja') or (source_lang == 'ja' and target_lang == 'en'):
            self.source_processor = EnJaProcessor(source_lang)
            self.target_processor = EnJaProcessor(target_lang)
        else:
            if source_lang == 'zh':
                self.source_processor = ChineseProcessor()
            if target_lang == 'zh':
                self.target_processor = ChineseProcessor()
            if source_lang is not None and source_lang not in ['ja', 'zh']:
                self.source_processor = MosesProcessor(source_lang)
            if target_lang is not None and target_lang not in ['ja', 'zh']:
                self.target_processor = MosesProcessor(target_lang)

    @torch.no_grad()
    def batch_translate(
        self, src: torch.LongTensor, src_mask: torch.LongTensor,
    ):
        """	
        Translates a minibatch of inputs from source language to target language.	
        Args:	
            src: minibatch of inputs in the src language (batch x seq_len)	
            src_mask: mask tensor indicating elements to be ignored (batch x seq_len)	
        Returns:	
            translations: a list strings containing detokenized translations	
            inputs: a list of string containing detokenized inputs	
        """
        mode = self.training
        try:
            self.eval()
            src_hiddens = self.encoder(input_ids=src, encoder_mask=src_mask)
            beam_results = self.beam_search(encoder_hidden_states=src_hiddens, encoder_input_mask=src_mask)
            beam_results = self.filter_predicted_ids(beam_results)

            translations = [self.decoder_tokenizer.ids_to_text(tr) for tr in beam_results.cpu().numpy()]
            inputs = [self.encoder_tokenizer.ids_to_text(inp) for inp in src.cpu().numpy()]
            if self.target_processor is not None:
                translations = [
                    self.target_processor.detokenize(translation.split(' ')) for translation in translations
                ]

            if self.source_processor is not None:
                inputs = [self.source_processor.detokenize(item.split(' ')) for item in inputs]
        finally:
            self.train(mode=mode)
        return inputs, translations

    # TODO: We should drop source/target_lang arguments in favor of using self.src/tgt_language
    @torch.no_grad()
    def translate(self, text: List[str], source_lang: str = None, target_lang: str = None) -> List[str]:
        """
        Translates list of sentences from source language to target language.
        Should be regular text, this method performs its own tokenization/de-tokenization
        Args:
            text: list of strings to translate
            source_lang: if not None, corresponding MosesTokenizer and MosesPunctNormalizer will be run
            target_lang: if not None, corresponding MosesDecokenizer will be run
        Returns:
            list of translated strings
        """
        # __TODO__: This will reset both source and target processors even if you want to reset just one.
        if source_lang is not None or target_lang is not None:
            self.setup_pre_and_post_processing_utils(source_lang, target_lang)

        mode = self.training
        try:
            self.eval()
            inputs = []
            for txt in text:
                if self.source_processor is not None:
                    txt = self.source_processor.normalize(txt)
                    txt = self.source_processor.tokenize(txt)
                ids = self.encoder_tokenizer.text_to_ids(txt)
                ids = [self.encoder_tokenizer.bos_id] + ids + [self.encoder_tokenizer.eos_id]
                inputs.append(ids)
            max_len = max(len(txt) for txt in inputs)
            src_ids_ = np.ones((len(inputs), max_len)) * self.encoder_tokenizer.pad_id
            for i, txt in enumerate(inputs):
                src_ids_[i][: len(txt)] = txt

            src_mask = torch.FloatTensor((src_ids_ != self.encoder_tokenizer.pad_id)).to(self.device)
            src = torch.LongTensor(src_ids_).to(self.device)
            _, translations = self.batch_translate(src, src_mask)
        finally:
            self.train(mode=mode)
        return translations

    @classmethod
    def list_available_models(cls) -> Optional[Dict[str, str]]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        model = PretrainedModelInfo(
            pretrained_model_name="nmt_en_de_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_en_de_transformer12x2/versions/1.0.0rc1/files/nmt_en_de_transformer12x2.nemo",
            description="En->De translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_en_de_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_de_en_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_de_en_transformer12x2/versions/1.0.0rc1/files/nmt_de_en_transformer12x2.nemo",
            description="De->En translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_de_en_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_en_es_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_en_es_transformer12x2/versions/1.0.0rc1/files/nmt_en_es_transformer12x2.nemo",
            description="En->Es translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_en_es_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_es_en_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_es_en_transformer12x2/versions/1.0.0rc1/files/nmt_es_en_transformer12x2.nemo",
            description="Es->En translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_es_en_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_en_fr_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_en_fr_transformer12x2/versions/1.0.0rc1/files/nmt_en_fr_transformer12x2.nemo",
            description="En->Fr translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_en_fr_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_fr_en_transformer12x2",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_fr_en_transformer12x2/versions/1.0.0rc1/files/nmt_fr_en_transformer12x2.nemo",
            description="Fr->En translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_fr_en_transformer12x2",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_en_ru_transformer6x6",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_en_ru_transformer6x6/versions/1.0.0rc1/files/nmt_en_ru_transformer6x6.nemo",
            description="En->Ru translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_en_ru_transformer6x6",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_ru_en_transformer6x6",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_ru_en_transformer6x6/versions/1.0.0rc1/files/nmt_ru_en_transformer6x6.nemo",
            description="Ru->En translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_ru_en_transformer6x6",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_zh_en_transformer6x6",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_zh_en_transformer6x6/versions/1.0.0rc1/files/nmt_zh_en_transformer6x6.nemo",
            description="Zh->En translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_zh_en_transformer6x6",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="nmt_en_zh_transformer6x6",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/nmt_en_zh_transformer6x6/versions/1.0.0rc1/files/nmt_en_zh_transformer6x6.nemo",
            description="En->Zh translation model. See details here: https://ngc.nvidia.com/catalog/models/nvidia:nemo:nmt_en_zh_transformer6x6",
        )
        result.append(model)

        return result

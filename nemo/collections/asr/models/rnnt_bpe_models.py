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

import copy
import os
from typing import Dict, Optional

import torch
from omegaconf import DictConfig, ListConfig, OmegaConf, open_dict
from pytorch_lightning import Trainer

from nemo.collections.asr.data import audio_to_text_dataset
from nemo.collections.asr.losses.rnnt import RNNTLoss
from nemo.collections.asr.metrics.rnnt_wer_bpe import RNNTBPEWER, RNNTBPEDecoding
from nemo.collections.asr.models.rnnt_models import EncDecRNNTModel
from nemo.collections.asr.parts.mixins import ASRBPEMixin
from nemo.collections.asr.parts.perturb import process_augmentations
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import logging, model_utils

try:
    import warprnnt_pytorch as warprnnt

    WARP_RNNT_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    WARP_RNNT_AVAILABLE = False


class EncDecRNNTBPEModel(EncDecRNNTModel, ASRBPEMixin):
    """Base class for encoder decoder RNNT-based models with subword tokenization."""

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        return result

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # Required loss function
        if not WARP_RNNT_AVAILABLE:
            raise ImportError(
                "Could not import `warprnnt_pytorch`.\n"
                "Please visit https://github.com/HawkAaron/warp-transducer "
                "and follow the steps in the readme to build and install the "
                "pytorch bindings for RNNT Loss, or use the provided docker "
                "container that supports RNN-T loss."
            )

        # Convert to Hydra 1.0 compatible DictConfig
        cfg = model_utils.convert_model_config_to_dict_config(cfg)
        cfg = model_utils.maybe_update_config_version(cfg)

        # Tokenizer is necessary for this model
        if 'tokenizer' not in cfg:
            raise ValueError("`cfg` must have `tokenizer` config to create a tokenizer !")

        if not isinstance(cfg, DictConfig):
            cfg = OmegaConf.create(cfg)

        # Setup the tokenizer
        self._setup_tokenizer(cfg.tokenizer)

        # Initialize a dummy vocabulary
        vocabulary = self.tokenizer.tokenizer.get_vocab()

        # Set the new vocabulary
        with open_dict(cfg):
            cfg.labels = ListConfig(list(vocabulary))

        with open_dict(cfg.decoder):
            cfg.decoder.vocab_size = len(vocabulary)

        with open_dict(cfg.joint):
            cfg.joint.num_classes = len(vocabulary)
            cfg.joint.vocabulary = ListConfig(list(vocabulary))
            cfg.joint.jointnet.encoder_hidden = cfg.model_defaults.enc_hidden
            cfg.joint.jointnet.pred_hidden = cfg.model_defaults.pred_hidden

        super().__init__(cfg=cfg, trainer=trainer)

        # Setup decoding object
        self.decoding = RNNTBPEDecoding(
            decoding_cfg=self.cfg.decoding, decoder=self.decoder, joint=self.joint, tokenizer=self.tokenizer,
        )

        # Setup wer object
        self.wer = RNNTBPEWER(
            decoding=self.decoding, batch_dim_index=0, use_cer=False, log_prediction=True, dist_sync_on_step=True
        )

        # Setup fused Joint step if flag is set
        if self.joint.fuse_loss_wer:
            self.joint.set_loss(self.loss)
            self.joint.set_wer(self.wer)

    def change_vocabulary(
        self, new_tokenizer_dir: str, new_tokenizer_type: str, decoding_cfg: Optional[DictConfig] = None
    ):
        """
        Changes vocabulary used during RNNT decoding process. Use this method when fine-tuning on from pre-trained model.
        This method changes only decoder and leaves encoder and pre-processing modules unchanged. For example, you would
        use it if you want to use pretrained encoder when fine-tuning on data in another language, or when you'd need
        model to learn capitalization, punctuation and/or special characters.

        Args:
            new_tokenizer_dir: Directory path to tokenizer.
            new_tokenizer_type: Type of tokenizer. Can be either `bpe` or `wpe`.
            decoding_cfg: A config for the decoder, which is optional. If the decoding type
                needs to be changed (from say Greedy to Beam decoding etc), the config can be passed here.

        Returns: None

        """
        if not os.path.isdir(new_tokenizer_dir):
            raise NotADirectoryError(
                f'New tokenizer dir must be non-empty path to a directory. But I got: {new_tokenizer_dir}'
            )

        if new_tokenizer_type.lower() not in ('bpe', 'wpe'):
            raise ValueError(f'New tokenizer type must be either `bpe` or `wpe`')

        tokenizer_cfg = OmegaConf.create({'dir': new_tokenizer_dir, 'type': new_tokenizer_type})

        # Setup the tokenizer
        self._setup_tokenizer(tokenizer_cfg)

        # Initialize a dummy vocabulary
        vocabulary = self.tokenizer.tokenizer.get_vocab()

        joint_config = self.joint.to_config_dict()
        new_joint_config = copy.deepcopy(joint_config)
        new_joint_config['vocabulary'] = ListConfig(list(vocabulary.values()))
        new_joint_config['num_classes'] = len(vocabulary)
        del self.joint
        self.joint = EncDecRNNTBPEModel.from_config_dict(new_joint_config)

        decoder_config = self.decoder.to_config_dict()
        new_decoder_config = copy.deepcopy(decoder_config)
        new_decoder_config.vocab_size = len(vocabulary)
        del self.decoder
        self.decoder = EncDecRNNTBPEModel.from_config_dict(new_decoder_config)

        del self.loss
        self.loss = RNNTLoss(num_classes=self.joint.num_classes_with_blank - 1)

        if decoding_cfg is None:
            # Assume same decoding config as before
            decoding_cfg = self.cfg.decoding

        self.decoding = RNNTBPEDecoding(
            decoding_cfg=decoding_cfg, decoder=self.decoder, joint=self.joint, tokenizer=self.tokenizer,
        )

        self.wer = RNNTBPEWER(
            decoding=self.decoding,
            batch_dim_index=self.wer.batch_dim_index,
            use_cer=self.wer.use_cer,
            log_prediction=self.wer.log_prediction,
            dist_sync_on_step=True,
        )

        # Setup fused Joint step
        if self.joint.fuse_loss_wer:
            self.joint.set_loss(self.loss)
            self.joint.set_wer(self.wer)

        # Update config
        with open_dict(self.cfg.joint):
            self.cfg.joint = new_joint_config

        with open_dict(self.cfg.decoder):
            self.cfg.decoder = new_decoder_config

        with open_dict(self.cfg.decoding):
            self.cfg.decoding = decoding_cfg

        logging.info(f"Changed decoder to output to {self.joint.vocabulary} vocabulary.")

    def _setup_dataloader_from_config(self, config: Optional[Dict]):
        if 'augmentor' in config:
            augmentor = process_augmentations(config['augmentor'])
        else:
            augmentor = None

        shuffle = config['shuffle']

        # Instantiate tarred dataset loader or normal dataset loader
        if config.get('is_tarred', False):
            if ('tarred_audio_filepaths' in config and config['tarred_audio_filepaths'] is None) or (
                'manifest_filepath' in config and config['manifest_filepath'] is None
            ):
                logging.warning(
                    "Could not load dataset as `manifest_filepath` was None or "
                    f"`tarred_audio_filepaths` is None. Provided config : {config}"
                )
                return None

            shuffle_n = config.get('shuffle_n', 4 * config['batch_size']) if shuffle else 0
            dataset = audio_to_text_dataset.get_tarred_bpe_dataset(
                config=config,
                tokenizer=self.tokenizer,
                shuffle_n=shuffle_n,
                global_rank=self.global_rank,
                world_size=self.world_size,
                augmentor=augmentor,
            )
            shuffle = False
        else:
            if 'manifest_filepath' in config and config['manifest_filepath'] is None:
                logging.warning(f"Could not load dataset as `manifest_filepath` was None. Provided config : {config}")
                return None

            dataset = audio_to_text_dataset.get_bpe_dataset(
                config=config, tokenizer=self.tokenizer, augmentor=augmentor
            )

        return torch.utils.data.DataLoader(
            dataset=dataset,
            batch_size=config['batch_size'],
            collate_fn=dataset.collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=shuffle,
            num_workers=config.get('num_workers', 0),
            pin_memory=config.get('pin_memory', False),
        )

    def _setup_transcribe_dataloader(self, config: Dict) -> 'torch.utils.data.DataLoader':
        """
        Setup function for a temporary data loader which wraps the provided audio file.

        Args:
            config: A python dictionary which contains the following keys:
            paths2audio_files: (a list) of paths to audio files. The files should be relatively short fragments. \
                Recommended length per file is between 5 and 25 seconds.
            batch_size: (int) batch size to use during inference. \
                Bigger will result in better throughput performance but would use more memory.
            temp_dir: (str) A temporary directory where the audio manifest is temporarily
                stored.

        Returns:
            A pytorch DataLoader for the given audio file(s).
        """
        dl_config = {
            'manifest_filepath': os.path.join(config['temp_dir'], 'manifest.json'),
            'sample_rate': self.preprocessor._sample_rate,
            'batch_size': min(config['batch_size'], len(config['paths2audio_files'])),
            'shuffle': False,
        }

        temporary_datalayer = self._setup_dataloader_from_config(config=DictConfig(dl_config))
        return temporary_datalayer

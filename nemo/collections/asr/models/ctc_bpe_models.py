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

from nemo.collections.asr.data.audio_to_text import AudioToBPEDataset, TarredAudioToBPEDataset
from nemo.collections.asr.losses.ctc import CTCLoss
from nemo.collections.asr.metrics.wer_bpe import WERBPE
from nemo.collections.asr.models.ctc_models import EncDecCTCModel
from nemo.collections.asr.parts.perturb import process_augmentations
from nemo.collections.common import tokenizers
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import logging

__all__ = ['EncDecCTCModelBPE', 'JasperNetBPE', 'QuartzNetBPE']


class EncDecCTCModelBPE(EncDecCTCModel):
    """Encoder decoder CTC-based models with Byte Pair Encoding."""

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        model = PretrainedModelInfo(
            pretrained_model_name="ContextNet-192-WPE-1024-8x-Stride",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemospeechmodels/versions/1.0.0a5/files/ContextNet-192-WPE-1024-8x-Stride.nemo",
            description="ContextNet initial implementation with CTC loss model trained on the Librispeech corpus and achieves a WER of 10.09% on test-other and 10.11% on dev-other.",
        )
        result.append(model)
        return result

    def __init__(self, cfg: DictConfig, trainer=None):
        if 'tokenizer' not in cfg:
            raise ValueError("`cfg` must have `tokenizer` config to create a tokenizer !")

        self.tokenizer_cfg = OmegaConf.to_container(cfg.tokenizer, resolve=True)  # type: dict
        self.tokenizer_dir = self.tokenizer_cfg.pop('dir')  # Remove tokenizer directory
        self.tokenizer_type = self.tokenizer_cfg.pop('type').lower()  # Remove tokenizer_type

        # Setup the tokenizer
        self._setup_tokenizer()

        # Initialize a dummy vocabulary
        vocabulary = self.tokenizer.tokenizer.get_vocab()

        # Set the new vocabulary
        with open_dict(cfg):
            if "params" in cfg.decoder:
                cfg.decoder.params.vocabulary = ListConfig(list(vocabulary.values()))
            else:
                cfg.decoder.vocabulary = ListConfig(list(vocabulary.values()))

        # Override number of classes if placeholder provided
        if "params" in cfg.decoder:
            num_classes = cfg.decoder["params"]["num_classes"]
        else:
            num_classes = cfg.decoder["num_classes"]

        if num_classes < 1:
            logging.info(
                "\nReplacing placeholder number of classes ({}) with actual number of classes - {}".format(
                    num_classes, len(vocabulary)
                )
            )
            if "params" in cfg.decoder:
                cfg.decoder["params"]["num_classes"] = len(vocabulary)
            else:
                cfg.decoder["num_classes"] = len(vocabulary)

        super().__init__(cfg=cfg, trainer=trainer)

        # Setup metric objects
        self._wer = WERBPE(
            tokenizer=self.tokenizer,
            batch_dim_index=0,
            use_cer=self._cfg.get('use_cer', False),
            ctc_decode=True,
            dist_sync_on_step=True,
            log_prediction=self._cfg.get("log_prediction", False),
        )

    def _setup_tokenizer(self):
        if self.tokenizer_type not in ['bpe', 'wpe']:
            raise ValueError(
                "`tokenizer.type` must be either `bpe` for SentencePiece tokenizer or "
                "`wpe` for BERT based tokenizer"
            )

        if self.tokenizer_type == 'bpe':
            # This is a BPE Tokenizer
            model_path = os.path.join(self.tokenizer_dir, 'tokenizer.model')
            model_path = self.register_artifact('tokenizer.model_path', model_path)
            self.model_path = model_path

            if 'special_tokens' in self.tokenizer_cfg:
                special_tokens = self.tokenizer_cfg['special_tokens']
            else:
                special_tokens = None

            # Update special tokens
            self.tokenizer = tokenizers.SentencePieceTokenizer(model_path=model_path, special_tokens=special_tokens)

            vocab_path = os.path.join(self.tokenizer_dir, 'vocab.txt')
            vocab_path = self.register_artifact('tokenizer.vocab_path', vocab_path)
            self.vocab_path = vocab_path

            vocabulary = {0: '<unk>'}
            with open(vocab_path) as f:
                for i, piece in enumerate(f):
                    piece = piece.replace('\n', '')
                    vocabulary[i + 1] = piece

            # wrapper method to get vocabulary conveniently
            def get_vocab():
                return vocabulary

            # attach utility values to the tokenizer wrapper
            self.tokenizer.tokenizer.vocab_size = len(vocabulary)
            self.tokenizer.tokenizer.get_vocab = get_vocab
            self.tokenizer.tokenizer.all_special_tokens = self.tokenizer.special_token_to_id

        else:
            # This is a WPE Tokenizer
            vocab_path = os.path.join(self.tokenizer_dir, 'vocab.txt')
            self.tokenizer_dir = self.register_artifact('tokenizer.vocab_path', vocab_path)
            self.vocab_path = self.tokenizer_dir

            self.tokenizer = tokenizers.AutoTokenizer(
                pretrained_model_name='bert-base-cased', vocab_file=self.tokenizer_dir, **self.tokenizer_cfg
            )
        logging.info(
            "Tokenizer {} initialized with {} tokens".format(
                self.tokenizer.__class__.__name__, self.tokenizer.vocab_size
            )
        )

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

            shuffle_n = config.get('shuffle_n', 4 * config['batch_size'])
            dataset = TarredAudioToBPEDataset(
                audio_tar_filepaths=config['tarred_audio_filepaths'],
                manifest_filepath=config['manifest_filepath'],
                tokenizer=self.tokenizer,
                sample_rate=config['sample_rate'],
                int_values=config.get('int_values', False),
                augmentor=augmentor,
                shuffle_n=shuffle_n,
                max_duration=config.get('max_duration', None),
                min_duration=config.get('min_duration', None),
                max_utts=config.get('max_utts', 0),
                trim=config.get('trim_silence', True),
                add_misc=config.get('add_misc', False),
                global_rank=self.global_rank,
                world_size=self.world_size,
                use_start_end_token=config.get('use_start_end_token', True),
            )
            shuffle = False
        else:
            if 'manifest_filepath' in config and config['manifest_filepath'] is None:
                logging.warning(f"Could not load dataset as `manifest_filepath` was None. Provided config : {config}")
                return None

            dataset = AudioToBPEDataset(
                manifest_filepath=config['manifest_filepath'],
                tokenizer=self.tokenizer,
                sample_rate=config['sample_rate'],
                int_values=config.get('int_values', False),
                augmentor=augmentor,
                max_duration=config.get('max_duration', None),
                min_duration=config.get('min_duration', None),
                max_utts=config.get('max_utts', 0),
                trim=config.get('trim_silence', True),
                load_audio=config.get('load_audio', True),
                add_misc=config.get('add_misc', False),
                use_start_end_token=config.get('use_start_end_token', True),
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

    def change_vocabulary(self, new_tokenizer_dir: str, new_tokenizer_type: str):
        """
        Changes vocabulary of the tokenizer used during CTC decoding process.
        Use this method when fine-tuning on from pre-trained model.
        This method changes only decoder and leaves encoder and pre-processing modules unchanged. For example, you would
        use it if you want to use pretrained encoder when fine-tuning on a data in another language, or when you'd need
        model to learn capitalization, punctuation and/or special characters.

        Args:
            new_tokenizer_dir: Path to the new tokenizer directory.
            new_tokenizer_type: Either `bpe` or `wpe`. `bpe` is used for SentencePiece tokenizers,
                whereas `wpe` is used for `BertTokenizer`.

        Returns: None

        """
        if not os.path.isdir(new_tokenizer_dir):
            raise NotADirectoryError(
                f'New tokenizer dir must be non-empty path to a directory. But I got: {new_tokenizer_dir}'
            )

        if new_tokenizer_type.lower() not in ('bpe', 'wpe'):
            raise ValueError(f'New tokenizer type must be either `bpe` or `wpe`')

        self.tokenizer_dir = new_tokenizer_dir  # Remove tokenizer directory
        self.tokenizer_type = new_tokenizer_type.lower()  # Remove tokenizer_type

        # Setup the tokenizer
        self._setup_tokenizer()

        # Initialize a dummy vocabulary
        vocabulary = self.tokenizer.tokenizer.get_vocab()

        # Set the new vocabulary
        decoder_config = copy.deepcopy(self.decoder.to_config_dict())
        decoder_config.vocabulary = ListConfig(list(vocabulary.values()))

        if "params" in decoder_config:
            decoder_num_classes = decoder_config['params']['num_classes']
        else:
            decoder_num_classes = decoder_config['num_classes']

        # Override number of classes if placeholder provided
        logging.info(
            "\nReplacing old number of classes ({}) with new number of classes - {}".format(
                decoder_num_classes, len(vocabulary)
            )
        )

        if "params" in decoder_config:
            decoder_config['params']['num_classes'] = len(vocabulary)
        else:
            decoder_config['num_classes'] = len(vocabulary)

        del self.decoder
        self.decoder = EncDecCTCModelBPE.from_config_dict(decoder_config)
        del self.loss
        self.loss = CTCLoss(
            num_classes=self.decoder.num_classes_with_blank - 1,
            zero_infinity=True,
            reduction=self._cfg.get("ctc_reduction", "mean_batch"),
        )
        self._wer = WERBPE(
            tokenizer=self.tokenizer,
            batch_dim_index=0,
            use_cer=self._cfg.get('use_cer', False),
            ctc_decode=True,
            log_prediction=self._cfg.get("log_prediction", False),
        )

        # Update config
        OmegaConf.set_struct(self._cfg.decoder, False)
        self._cfg.decoder = decoder_config
        OmegaConf.set_struct(self._cfg.decoder, True)

        logging.info(f"Changed tokenizer to {self.decoder.vocabulary} vocabulary.")


class JasperNetBPE(EncDecCTCModelBPE):
    pass


class QuartzNetBPE(EncDecCTCModelBPE):
    pass

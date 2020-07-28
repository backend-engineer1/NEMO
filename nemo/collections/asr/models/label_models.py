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

from typing import Dict, Optional, Union

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer

from nemo.collections.asr.data.audio_to_label import AudioToSpeechLabelDataSet
from nemo.collections.asr.parts.features import WaveformFeaturizer
from nemo.collections.common.losses import CrossEntropyLoss as CELoss
from nemo.core.classes import ModelPT
from nemo.core.classes.common import typecheck
from nemo.core.neural_types import *
from nemo.utils import logging
from nemo.utils.decorators import experimental

__all__ = ['EncDecSpeakerLabelModel']


@experimental
class EncDecSpeakerLabelModel(ModelPT):
    """Encoder decoder class for speaker label models.
    Model class creates training, validation methods for setting up data
    performing model forward pass. 
    Expects config dict for 
    * preprocessor
    * Jasper/Quartznet Encoder
    * Speaker Decoder 
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        # if 'cls' not in cfg:
        #     # This is for Jarvis service. Adding here for now to avoid effects of decorators
        #     OmegaConf.set_struct(cfg, False)
        #     cfg.cls = 'nemo.collections.asr.models.EncDecSpeakerLabelModel'
        #     OmegaConf.set_struct(cfg, True)

        super().__init__(cfg=cfg, trainer=trainer)
        self.preprocessor = EncDecSpeakerLabelModel.from_config_dict(cfg.preprocessor)
        self.encoder = EncDecSpeakerLabelModel.from_config_dict(cfg.encoder)
        self.decoder = EncDecSpeakerLabelModel.from_config_dict(cfg.decoder)
        self.loss = CELoss()
        # Optimizer setup needs to happen after all model weights are ready
        self.setup_optimization()

    def __setup_dataloader_from_config(self, config: Optional[Dict]):
        featurizer = WaveformFeaturizer(sample_rate=config['sample_rate'], int_values=config.get('int_values', False))
        self.dataset = AudioToSpeechLabelDataSet(
            manifest_filepath=config['manifest_filepath'],
            labels=config['labels'],
            featurizer=featurizer,
            max_duration=config.get('max_duration', None),
            min_duration=config.get('min_duration', None),
            trim=config.get('trim_silence', True),
            load_audio=config.get('load_audio', True),
            time_length=config.get('time_length', 8),
        )

        return torch.utils.data.DataLoader(
            dataset=self.dataset,
            batch_size=config['batch_size'],
            collate_fn=self.dataset.fixed_seq_collate_fn,
            drop_last=config.get('drop_last', False),
            shuffle=config['shuffle'],
            num_workers=config.get('num_workers', 2),
        )

    def setup_training_data(self, train_data_layer_config: Optional[Union[DictConfig, Dict]]):
        if 'shuffle' not in train_data_layer_config:
            train_data_layer_config['shuffle'] = True
        self._train_dl = self.__setup_dataloader_from_config(config=train_data_layer_config)

    def setup_validation_data(self, val_data_layer_config: Optional[Union[DictConfig, Dict]]):
        if 'shuffle' not in val_data_layer_config:
            val_data_layer_config['shuffle'] = False
        val_data_layer_config['labels'] = self.dataset.labels
        self._validation_dl = self.__setup_dataloader_from_config(config=val_data_layer_config)

    def setup_test_data(self, test_data_layer_params: Optional[Union[DictConfig, Dict]]):
        if 'shuffle' not in test_data_layer_params:
            test_data_layer_params['shuffle'] = False
        self._test_dl = self.__setup_dataloader_from_config(config=test_data_layer_params)

    @classmethod
    def list_available_models(cls) -> Optional[Dict[str, str]]:
        pass

    @classmethod
    def from_pretrained(cls, name: str):
        pass

    def export(self, **kwargs):
        pass

    def save_to(self, save_path: str):
        pass

    @classmethod
    def restore_from(cls, restore_path: str):
        pass

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        if hasattr(self.preprocessor, '_sample_rate'):
            audio_eltype = AudioSignal(freq=self.preprocessor._sample_rate)
        else:
            audio_eltype = AudioSignal()
        return {
            "input_signal": NeuralType(('B', 'T'), audio_eltype),
            "input_signal_length": NeuralType(tuple('B'), LengthsType()),
        }

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return {
            "logits": NeuralType(('B', 'D'), LogitsType()),
            "embs": NeuralType(('B', 'D'), AcousticEncodedRepresentation()),
        }

    @typecheck()
    def forward(self, input_signal, input_signal_length):
        processed_signal, processed_signal_len = self.preprocessor(
            input_signal=input_signal, length=input_signal_length,
        )

        encoded, _ = self.encoder(audio_signal=processed_signal, length=processed_signal_len)
        logits, embs = self.decoder(encoder_output=encoded)
        return logits, embs

    # PTL-specific methods
    def training_step(self, batch, batch_nb):
        self.train()
        audio_signal, audio_signal_len, labels, _ = batch
        logits, _ = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        loss_value = self.loss(logits=logits, labels=labels)
        labels_hat = torch.argmax(logits, dim=1)
        n_correct_pred = torch.sum(labels == labels_hat, dim=0).item()
        tensorboard_logs = {'train_loss': loss_value, 'training_batch_acc': (n_correct_pred / len(labels)) * 100}

        return {'loss': loss_value, 'log': tensorboard_logs, "n_correct_pred": n_correct_pred, "n_pred": len(labels)}

    def validation_step(self, batch, batch_idx):
        self.eval()
        audio_signal, audio_signal_len, labels, _ = batch
        logits, _ = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        loss_value = self.loss(logits=logits, labels=labels)
        labels_hat = torch.argmax(logits, dim=1)
        n_correct_pred = torch.sum(labels == labels_hat, dim=0).item()

        return {'val_loss': loss_value, "n_correct_pred": n_correct_pred, "n_pred": len(labels)}

    def validation_epoch_end(self, outputs):
        val_loss_mean = torch.stack([x['val_loss'] for x in outputs]).mean()
        val_acc = (sum([x['n_correct_pred'] for x in outputs]) / sum(x['n_pred'] for x in outputs)) * 100
        logging.info("validation accuracy {:.3f}".format(val_acc))
        tensorboard_logs = {'validation_loss': val_loss_mean, 'validation_acc': val_acc}

        return {'val_loss': val_loss_mean, 'log': tensorboard_logs}

    def training_epoch_end(self, outputs):
        train_acc = (sum([x['n_correct_pred'] for x in outputs]) / sum(x['n_pred'] for x in outputs)) * 100
        logging.info("training accuracy {:.3f}".format(train_acc))

        return {}

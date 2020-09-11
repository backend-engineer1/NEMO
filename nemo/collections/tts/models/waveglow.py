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

from dataclasses import dataclass
from typing import Any, Dict, Optional

import torch
from hydra.utils import instantiate
from omegaconf import MISSING, DictConfig, OmegaConf, open_dict

from nemo.collections.tts.helpers.helpers import waveglow_log_to_tb_func
from nemo.collections.tts.losses.waveglowloss import WaveGlowLoss
from nemo.collections.tts.models.base import Vocoder
from nemo.collections.tts.modules.waveglow import OperationMode
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.neural_types.elements import (
    AudioSignal,
    LengthsType,
    MelSpectrogramType,
    NormalDistributionSamplesType,
    VoidType,
)
from nemo.core.neural_types.neural_type import NeuralType
from nemo.utils import logging


@dataclass
class PreprocessorParams:
    pad_value: float = MISSING


@dataclass
class Preprocessor:
    cls: str = MISSING
    params: PreprocessorParams = PreprocessorParams()


@dataclass
class WaveglowConfig:
    waveglow: Dict[Any, Any] = MISSING
    preprocessor: Preprocessor = Preprocessor()
    sigma: float = MISSING
    train_ds: Optional[Dict[Any, Any]] = None
    validation_ds: Optional[Dict[Any, Any]] = None


class WaveGlowModel(Vocoder):
    """Waveglow model used to convert betweeen spectrograms and audio"""

    def __init__(self, cfg: DictConfig, trainer: 'Trainer' = None):
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)
        super().__init__(cfg=cfg, trainer=trainer)

        schema = OmegaConf.structured(WaveglowConfig)
        # ModelPT ensures that cfg is a DictConfig, but do this second check in case ModelPT changes
        if isinstance(cfg, dict):
            cfg = OmegaConf.create(cfg)
        elif not isinstance(cfg, DictConfig):
            raise ValueError(f"cfg was type: {type(cfg)}. Expected either a dict or a DictConfig")
        # Ensure passed cfg is compliant with schema
        OmegaConf.merge(cfg, schema)

        self.pad_value = self._cfg.preprocessor.params.pad_value
        self.sigma = self._cfg.sigma
        self.audio_to_melspec_precessor = instantiate(self._cfg.preprocessor)
        self.waveglow = instantiate(self._cfg.waveglow)
        self.mode = OperationMode.infer
        self.loss = WaveGlowLoss()
        self.removed_weightnorm = False

    @property
    def input_types(self):
        return {
            "audio": NeuralType(('B', 'T'), AudioSignal()),
            "audio_len": NeuralType(('B'), LengthsType()),
            "run_inverse": NeuralType(optional=True),
        }

    @property
    def output_types(self):
        if self.mode == OperationMode.training or self.mode == OperationMode.validation:
            output_dict = {
                "pred_normal_dist": NeuralType(('B', 'flowgroup', 'T'), NormalDistributionSamplesType()),
                "log_s_list": NeuralType(('B', 'flowgroup', 'T'), VoidType()),  # TODO: Figure out a good typing
                "log_det_W_list": NeuralType(elements_type=VoidType()),  # TODO: Figure out a good typing
            }
            if self.mode == OperationMode.validation:
                output_dict["audio_pred"] = NeuralType(('B', 'T'), AudioSignal())
                output_dict["spec"] = NeuralType(('B', 'T', 'D'), MelSpectrogramType())
                output_dict["spec_len"] = NeuralType(('B'), LengthsType())
            return output_dict
        return {
            "audio_pred": NeuralType(('B', 'T'), AudioSignal()),
        }

    @typecheck()
    def forward(self, *, audio, audio_len, run_inverse=True):
        if self.mode != self.waveglow.mode:
            raise ValueError(
                f"WaveGlowModel's mode {self.mode} does not match WaveGlowModule's mode {self.waveglow.mode}"
            )
        spec, spec_len = self.audio_to_melspec_precessor(audio, audio_len)
        tensors = self.waveglow(spec=spec, audio=audio, run_inverse=run_inverse, sigma=self.sigma)
        if self.mode == OperationMode.training:
            return tensors[:-1]  # z, log_s_list, log_det_W_list
        elif self.mode == OperationMode.validation:
            z, log_s_list, log_det_W_list, audio_pred = tensors
            return z, log_s_list, log_det_W_list, audio_pred, spec, spec_len
        return tensors  # audio_pred

    @typecheck(
        input_types={"spec": NeuralType(('B', 'D', 'T'), MelSpectrogramType()), "sigma": NeuralType(optional=True)},
        output_types={"audio": NeuralType(('B', 'T'), AudioSignal())},
    )
    def convert_spectrogram_to_audio(self, spec: torch.Tensor, sigma: float = 1.0) -> torch.Tensor:
        if not self.removed_weightnorm:
            self.waveglow.remove_weightnorm()
            self.removed_weightnorm = True
        self.eval()
        self.mode = OperationMode.infer
        self.waveglow.mode = OperationMode.infer

        with torch.no_grad():
            audio = self.waveglow(spec=spec, run_inverse=True, audio=None, sigma=sigma)

        return audio

    def training_step(self, batch, batch_idx):
        self.mode = OperationMode.training
        self.waveglow.mode = OperationMode.training
        audio, audio_len = batch
        z, log_s_list, log_det_W_list = self(audio=audio, audio_len=audio_len, run_inverse=False)

        loss = self.loss(z=z, log_s_list=log_s_list, log_det_W_list=log_det_W_list, sigma=self.sigma)
        output = {
            'loss': loss,
            'progress_bar': {'training_loss': loss},
            'log': {'loss': loss},
        }
        return output

    def validation_step(self, batch, batch_idx):
        self.mode = OperationMode.validation
        self.waveglow.mode = OperationMode.validation
        audio, audio_len = batch
        z, log_s_list, log_det_W_list, audio_pred, spec, spec_len = self(
            audio=audio, audio_len=audio_len, run_inverse=(batch_idx == 0)
        )
        loss = self.loss(z=z, log_s_list=log_s_list, log_det_W_list=log_det_W_list, sigma=self.sigma)
        return {
            "val_loss": loss,
            "audio_pred": audio_pred,
            "mel_target": spec,
            "mel_len": spec_len,
        }

    def validation_epoch_end(self, outputs):
        if self.logger is not None and self.logger.experiment is not None:
            waveglow_log_to_tb_func(
                self.logger.experiment,
                outputs[0].values(),
                self.global_step,
                tag="eval",
                mel_fb=self.audio_to_melspec_precessor.fb,
            )
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()
        tensorboard_logs = {'val_loss': avg_loss}
        return {'val_loss': avg_loss, 'log': tensorboard_logs}

    def __setup_dataloader_from_config(self, cfg, shuffle_should_be: bool = True, name: str = "train"):
        if "dataset" not in cfg or not isinstance(cfg.dataset, DictConfig):
            raise ValueError(f"No dataset for {name}")  # TODO
        if "dataloader_params" not in cfg or not isinstance(cfg.dataloader_params, DictConfig):
            raise ValueError(f"No dataloder_params for {name}")  # TODO
        if shuffle_should_be:
            if 'shuffle' not in cfg.dataloader_params:
                logging.warning(
                    f"Shuffle should be set to True for {self}'s {name} dataloader but was not found in its "
                    "config. Manually setting to True"
                )
                with open_dict(cfg["dataloader_params"]):
                    cfg.dataloader_params.shuffle = True
            elif not cfg.dataloader_params.shuffle:
                logging.error(f"The {name} dataloader for {self} has shuffle set to False!!!")
        elif not shuffle_should_be and cfg.dataloader_params.shuffle:
            logging.error(f"The {name} dataloader for {self} has shuffle set to True!!!")

        dataset = instantiate(cfg.dataset)
        return torch.utils.data.DataLoader(dataset, collate_fn=dataset.collate_fn, **cfg.dataloader_params)

    def setup_training_data(self, cfg):
        self._train_dl = self.__setup_dataloader_from_config(cfg)

    def setup_validation_data(self, cfg):
        self._validation_dl = self.__setup_dataloader_from_config(cfg, shuffle_should_be=False, name="validation")

    @classmethod
    def list_available_models(cls) -> 'List[PretrainedModelInfo]':
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        list_of_models = []
        model = PretrainedModelInfo(
            pretrained_model_name="WaveGlow-22050Hz",
            location="https://nemo-public.s3.us-east-2.amazonaws.com/nemo-1.0.0alpha-tests/waveglow.nemo",
            description="The model is trained on LJSpeech sampled at 22050Hz, and can be used as an universal vocoder",
            class_=cls,
        )
        list_of_models.append(model)
        return list_of_models

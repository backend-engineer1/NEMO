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
import json
import os
import pickle as pkl
from typing import Dict, List, Optional, Union

import onnx
import torch
from omegaconf import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning import Trainer

from nemo.collections.asr.data.audio_to_label import AudioToSpeechLabelDataset
from nemo.collections.asr.losses.angularloss import AngularSoftmaxLoss
from nemo.collections.asr.parts.features import WaveformFeaturizer
from nemo.collections.asr.parts.perturb import process_augmentations
from nemo.collections.common.losses import CrossEntropyLoss as CELoss
from nemo.collections.common.metrics import TopKClassificationAccuracy
from nemo.core.classes import ModelPT
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.classes.exportable import Exportable
from nemo.core.neural_types import *
from nemo.utils import logging
from nemo.utils.export_utils import attach_onnx_to_onnx

__all__ = ['EncDecSpeakerLabelModel', 'ExtractSpeakerEmbeddingsModel']


class EncDecSpeakerLabelModel(ModelPT, Exportable):
    """Encoder decoder class for speaker label models.
    Model class creates training, validation methods for setting up data
    performing model forward pass.
    Expects config dict for
    * preprocessor
    * Jasper/Quartznet Encoder
    * Speaker Decoder
    """

    @classmethod
    def list_available_models(cls) -> List[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        model = PretrainedModelInfo(
            pretrained_model_name="SpeakerNet_recognition",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemospeechmodels/versions/1.0.0a5/files/SpeakerNet_recognition.nemo",
            description="SpeakerNet_recognition model trained end-to-end for speaker recognition purposes with cross_entropy loss. It was trained on voxceleb 1, voxceleb 2 dev datasets and augmented with musan music and noise. Speaker Recognition model achieves 2.65% EER on voxceleb-O cleaned trial file",
        )
        result.append(model)

        model = PretrainedModelInfo(
            pretrained_model_name="SpeakerNet_verification",
            location="https://api.ngc.nvidia.com/v2/models/nvidia/nemospeechmodels/versions/1.0.0a5/files/SpeakerNet_verification.nemo",
            description="SpeakerNet_verification model trained end-to-end for speaker verification purposes with arcface angular softmax loss. It was trained on voxceleb 1, voxceleb 2 dev datasets and augmented with musan music and noise. Speaker Verification model achieves 2.12% EER on voxceleb-O cleaned trial file",
        )
        result.append(model)

        return result

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        super().__init__(cfg=cfg, trainer=trainer)
        self.preprocessor = EncDecSpeakerLabelModel.from_config_dict(cfg.preprocessor)
        self.encoder = EncDecSpeakerLabelModel.from_config_dict(cfg.encoder)
        self.decoder = EncDecSpeakerLabelModel.from_config_dict(cfg.decoder)
        if 'angular' in cfg.decoder and cfg.decoder['angular']:
            logging.info("Training with Angular Softmax Loss")
            scale = cfg.loss.scale
            margin = cfg.loss.margin
            self.loss = AngularSoftmaxLoss(scale=scale, margin=margin)
        else:
            logging.info("Training with Softmax-CrossEntropy loss")
            self.loss = CELoss()

        self._accuracy = TopKClassificationAccuracy(top_k=[1], dist_sync_on_step=True)

    def __setup_dataloader_from_config(self, config: Optional[Dict]):
        if 'augmentor' in config:
            augmentor = process_augmentations(config['augmentor'])
        else:
            augmentor = None

        featurizer = WaveformFeaturizer(
            sample_rate=config['sample_rate'], int_values=config.get('int_values', False), augmentor=augmentor
        )
        self.dataset = AudioToSpeechLabelDataset(
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
            pin_memory=config.get('pin_memory', False),
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
        if hasattr(self, 'dataset'):
            test_data_layer_params['labels'] = self.dataset.labels
        self.embedding_dir = test_data_layer_params.get('embedding_dir', './')
        self.test_manifest = test_data_layer_params.get('manifest_filepath', None)
        self._test_dl = self.__setup_dataloader_from_config(config=test_data_layer_params)

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
    def training_step(self, batch, batch_idx):
        audio_signal, audio_signal_len, labels, _ = batch
        logits, _ = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        loss = self.loss(logits=logits, labels=labels)

        self.log('loss', loss)
        self.log('learning_rate', self._optimizer.param_groups[0]['lr'])

        self._accuracy(logits=logits, labels=labels)
        top_k = self._accuracy.compute()
        for i, top_i in enumerate(top_k):
            self.log(f'training_batch_accuracy_top@{i}', top_i)

        return {'loss': loss}

    def validation_step(self, batch, batch_idx, dataloader_idx: int = 0):
        audio_signal, audio_signal_len, labels, _ = batch
        logits, _ = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        loss_value = self.loss(logits=logits, labels=labels)
        acc_top_k = self._accuracy(logits=logits, labels=labels)
        correct_counts, total_counts = self._accuracy.correct_counts_k, self._accuracy.total_counts_k

        return {
            'val_loss': loss_value,
            'val_correct_counts': correct_counts,
            'val_total_counts': total_counts,
            'val_acc_top_k': acc_top_k,
        }

    def multi_validation_epoch_end(self, outputs, dataloader_idx: int = 0):
        val_loss_mean = torch.stack([x['val_loss'] for x in outputs]).mean()
        correct_counts = torch.stack([x['val_correct_counts'] for x in outputs]).sum(axis=0)
        total_counts = torch.stack([x['val_total_counts'] for x in outputs]).sum(axis=0)

        self._accuracy.correct_counts_k = correct_counts
        self._accuracy.total_counts_k = total_counts
        topk_scores = self._accuracy.compute()

        logging.info("val_loss: {:.3f}".format(val_loss_mean))
        self.log('val_loss', val_loss_mean)
        for top_k, score in zip(self._accuracy.top_k, topk_scores):
            self.log('val_epoch_accuracy_top@{}'.format(top_k), score)

        return {
            'val_loss': val_loss_mean,
            'val_acc_top_k': topk_scores,
        }

    def test_step(self, batch, batch_idx, dataloader_idx: int = 0):
        audio_signal, audio_signal_len, labels, _ = batch
        logits, _ = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        loss_value = self.loss(logits=logits, labels=labels)
        acc_top_k = self._accuracy(logits=logits, labels=labels)
        correct_counts, total_counts = self._accuracy.correct_counts_k, self._accuracy.total_counts_k

        return {
            'test_loss': loss_value,
            'test_correct_counts': correct_counts,
            'test_total_counts': total_counts,
            'test_acc_top_k': acc_top_k,
        }

    def multi_test_epoch_end(self, outputs, dataloader_idx: int = 0):
        test_loss_mean = torch.stack([x['test_loss'] for x in outputs]).mean()
        correct_counts = torch.stack([x['test_correct_counts'] for x in outputs]).sum(axis=0)
        total_counts = torch.stack([x['test_total_counts'] for x in outputs]).sum(axis=0)

        self._accuracy.correct_counts_k = correct_counts
        self._accuracy.total_counts_k = total_counts
        topk_scores = self._accuracy.compute()

        logging.info("test_loss: {:.3f}".format(test_loss_mean))
        self.log('test_loss', test_loss_mean)
        for top_k, score in zip(self._accuracy.top_k, topk_scores):
            self.log('test_epoch_accuracy_top@{}'.format(top_k), score)

        return {
            'test_loss': test_loss_mean,
            'test_acc_top_k': topk_scores,
        }

    def setup_finetune_model(self, model_config: DictConfig):
        """
        setup_finetune_model method sets up training data, validation data and test data with new
        provided config, this checks for the previous labels set up during training from scratch, if None,
        it sets up labels for provided finetune data from manifest files

        Args:
        model_config: cfg which has train_ds, optional validation_ds, optional test_ds and
        mandatory encoder and decoder model params
        make sure you set num_classes correctly for finetune data

        Returns: None

        """
        if hasattr(self, 'dataset'):
            scratch_labels = self.dataset.labels
        else:
            scratch_labels = None

        logging.info("Setting up data loaders with manifests provided from model_config")

        if 'train_ds' in model_config and model_config.train_ds is not None:
            self.setup_training_data(model_config.train_ds)
        else:
            raise KeyError("train_ds is not found in model_config but you need it for fine tuning")

        if self.dataset.labels is None or len(self.dataset.labels) == 0:
            raise ValueError(f'New labels must be non-empty list of labels. But I got: {self.dataset.labels}')

        if 'validation_ds' in model_config and model_config.validation_ds is not None:
            self.setup_multiple_validation_data(model_config.validation_ds)

        if 'test_ds' in model_config and model_config.test_ds is not None:
            self.setup_multiple_test_data(model_config.test_ds)

        if scratch_labels == self.dataset.labels:  # checking for new finetune dataset labels
            logging.warning(
                "Trained dataset labels are same as finetune dataset labels -- continuing change of decoder parameters"
            )
        elif scratch_labels is None:
            logging.warning(
                "Either you provided a dummy manifest file during training from scratch or you restored from a pretrained nemo file"
            )

        decoder_config = model_config.decoder
        new_decoder_config = copy.deepcopy(decoder_config)
        if new_decoder_config['num_classes'] != len(self.dataset.labels):
            raise ValueError(
                "number of classes provided {} is not same as number of different labels in finetuning data: {}".format(
                    new_decoder_config['num_classes'], len(self.dataset.labels)
                )
            )

        del self.decoder
        self.decoder = EncDecSpeakerLabelModel.from_config_dict(new_decoder_config)

        with open_dict(self._cfg.decoder):
            self._cfg.decoder = new_decoder_config

        logging.info(f"Changed decoder output to # {self.decoder._num_classes} classes.")

    def export(
        self,
        output: str,
        input_example=None,
        output_example=None,
        verbose=False,
        export_params=True,
        do_constant_folding=True,
        keep_initializers_as_inputs=False,
        onnx_opset_version: int = 12,
        try_script: bool = False,
        set_eval: bool = True,
        check_trace: bool = True,
        use_dynamic_axes: bool = True,
    ):
        if input_example is not None or output_example is not None:
            logging.warning(
                "Passed input and output examples will be ignored and recomputed since"
                " EncDecSpeakerModel consists of two separate models (encoder and decoder) with different"
                " inputs and outputs."
            )

        qual_name = self.__module__ + '.' + self.__class__.__qualname__
        output1 = os.path.join(os.path.dirname(output), 'encoder_' + os.path.basename(output))
        output1_descr = qual_name + ' Encoder exported to ONNX'
        encoder_onnx = self.encoder.export(
            output1,
            None,  # computed by input_example()
            None,
            verbose,
            export_params,
            do_constant_folding,
            keep_initializers_as_inputs,
            onnx_opset_version,
            try_script,
            set_eval,
            check_trace,
            use_dynamic_axes,
        )

        output2 = os.path.join(os.path.dirname(output), 'decoder_' + os.path.basename(output))
        output2_descr = qual_name + ' Decoder exported to ONNX'
        decoder_onnx = self.decoder.export(
            output2,
            None,  # computed by input_example()
            None,
            verbose,
            export_params,
            do_constant_folding,
            keep_initializers_as_inputs,
            onnx_opset_version,
            try_script,
            set_eval,
            check_trace,
            use_dynamic_axes,
        )

        output_model = attach_onnx_to_onnx(encoder_onnx, decoder_onnx, "SL")
        output_descr = qual_name + ' Encoder+Decoder exported to ONNX'
        onnx.save(output_model, output)
        return ([output, output1, output2], [output_descr, output1_descr, output2_descr])


class ExtractSpeakerEmbeddingsModel(EncDecSpeakerLabelModel):
    """
    This Model class facilitates extraction of speaker embeddings from a pretrained model.
    Respective embedding file is saved in self.embedding dir passed through cfg
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        super().__init__(cfg=cfg, trainer=trainer)

    def test_step(self, batch, batch_ix):
        audio_signal, audio_signal_len, labels, slices = batch
        _, embs = self.forward(input_signal=audio_signal, input_signal_length=audio_signal_len)
        return {'embs': embs, 'labels': labels, 'slices': slices}

    def test_epoch_end(self, outputs):
        embs = torch.cat([x['embs'] for x in outputs])
        slices = torch.cat([x['slices'] for x in outputs])
        emb_shape = embs.shape[-1]
        embs = embs.view(-1, emb_shape).cpu().numpy()
        out_embeddings = {}
        start_idx = 0
        with open(self.test_manifest, 'r') as manifest:
            for idx, line in enumerate(manifest.readlines()):
                line = line.strip()
                dic = json.loads(line)
                structure = dic['audio_filepath'].split('/')[-3:]
                uniq_name = '@'.join(structure)
                if uniq_name in out_embeddings:
                    raise KeyError("Embeddings for label {} already present in emb dictionary".format(uniq_name))
                num_slices = slices[idx]
                end_idx = start_idx + num_slices
                out_embeddings[uniq_name] = embs[start_idx:end_idx].mean(axis=0)
                start_idx = end_idx

        embedding_dir = os.path.join(self.embedding_dir, 'embeddings')
        if not os.path.exists(embedding_dir):
            os.mkdir(embedding_dir)

        prefix = self.test_manifest.split('/')[-1].split('.')[-2]

        name = os.path.join(embedding_dir, prefix)
        pkl.dump(out_embeddings, open(name + '_embeddings.pkl', 'wb'))
        logging.info("Saved embedding files to {}".format(embedding_dir))

        return {}

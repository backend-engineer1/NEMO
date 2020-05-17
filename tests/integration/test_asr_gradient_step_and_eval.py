#!/usr/bin/env python
# -*- coding: utf-8 -*-
# =============================================================================
# Copyright 2020 NVIDIA. All Rights Reserved.
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
# =============================================================================

import os
import tarfile
from functools import partial
from unittest import TestCase

import pytest
from ruamel.yaml import YAML

import nemo
import nemo.collections.asr as nemo_asr

logging = nemo.logging


@pytest.mark.usefixtures("neural_factory")
class TestASRPytorch(TestCase):
    labels = [
        " ",
        "a",
        "b",
        "c",
        "d",
        "e",
        "f",
        "g",
        "h",
        "i",
        "j",
        "k",
        "l",
        "m",
        "n",
        "o",
        "p",
        "q",
        "r",
        "s",
        "t",
        "u",
        "v",
        "w",
        "x",
        "y",
        "z",
        "'",
    ]
    manifest_filepath = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/asr/an4_train.json"))
    featurizer_config = {
        'window': 'hann',
        'dither': 1e-05,
        'normalize': 'per_feature',
        'frame_splicing': 1,
        'int_values': False,
        'window_stride': 0.01,
        'sample_rate': 16000,
        'features': 64,
        'n_fft': 512,
        'window_size': 0.02,
    }
    yaml = YAML(typ="safe")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        data_folder = os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/"))
        logging.info("Looking up for test ASR data")
        if not os.path.exists(os.path.join(data_folder, "asr")):
            logging.info("Extracting ASR data to: {0}".format(os.path.join(data_folder, "asr")))
            tar = tarfile.open(os.path.join(data_folder, "asr.tar.gz"), "r:gz")
            tar.extractall(path=data_folder)
            tar.close()
        else:
            logging.info("ASR data found in: {0}".format(os.path.join(data_folder, "asr")))

    @staticmethod
    def print_and_log_loss(loss_tensor, loss_log_list):
        """A helper function that is passed to SimpleLossLoggerCallback. It prints loss_tensors and appends to
        the loss_log_list list.

        Args:
            loss_tensor (NMTensor): tensor representing loss. Loss should be a scalar
            loss_log_list (list): empty list
        """
        logging.info(f'Train Loss: {str(loss_tensor[0].item())}')
        loss_log_list.append(loss_tensor[0].item())

    @pytest.mark.integration
    def test_jasper_training(self):
        """Integtaion test that instantiates a small Jasper model and tests training with the sample asr data.
        Training is run for 3 forward and backward steps and asserts that loss after 3 steps is smaller than the loss
        at the first step.
        Note: Training is done with batch gradient descent as opposed to stochastic gradient descent due to CTC loss
        """
        with open(os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/jasper_smaller.yaml"))) as file:
            jasper_model_definition = self.yaml.load(file)
        dl = nemo_asr.AudioToTextDataLayer(
            # featurizer_config=self.featurizer_config,
            manifest_filepath=self.manifest_filepath,
            labels=self.labels,
            batch_size=30,
        )
        pre_process_params = {
            'frame_splicing': 1,
            'features': 64,
            'window_size': 0.02,
            'n_fft': 512,
            'dither': 1e-05,
            'window': 'hann',
            'sample_rate': 16000,
            'normalize': 'per_feature',
            'window_stride': 0.01,
        }
        preprocessing = nemo_asr.AudioToMelSpectrogramPreprocessor(**pre_process_params)
        jasper_encoder = nemo_asr.JasperEncoder(
            feat_in=jasper_model_definition['AudioToMelSpectrogramPreprocessor']['features'],
            **jasper_model_definition['JasperEncoder'],
        )
        jasper_decoder = nemo_asr.JasperDecoderForCTC(feat_in=1024, num_classes=len(self.labels))
        ctc_loss = nemo_asr.CTCLossNM(num_classes=len(self.labels))

        # DAG
        audio_signal, a_sig_length, transcript, transcript_len = dl()
        processed_signal, p_length = preprocessing(input_signal=audio_signal, length=a_sig_length)

        encoded, encoded_len = jasper_encoder(audio_signal=processed_signal, length=p_length)
        # logging.info(jasper_encoder)
        log_probs = jasper_decoder(encoder_output=encoded)
        loss = ctc_loss(
            log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len,
        )

        loss_list = []
        callback = nemo.core.SimpleLossLoggerCallback(
            tensors=[loss], print_func=partial(self.print_and_log_loss, loss_log_list=loss_list), step_freq=1
        )

        self.nf.train(
            [loss], callbacks=[callback], optimizer="sgd", optimization_params={"max_steps": 3, "lr": 0.001},
        )
        self.nf.reset_trainer()

        # Assert that training loss went down
        assert loss_list[-1] < loss_list[0]

    @pytest.mark.integration
    def test_quartznet_training(self):
        """Integtaion test that instantiates a small QuartzNet model and tests training with the sample asr data.
        Training is run for 3 forward and backward steps and asserts that loss after 3 steps is smaller than the loss
        at the first step.
        Note: Training is done with batch gradient descent as opposed to stochastic gradient descent due to CTC loss
        """
        with open(os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/quartznet_test.yaml"))) as f:
            quartz_model_definition = self.yaml.load(f)
        dl = nemo_asr.AudioToTextDataLayer(manifest_filepath=self.manifest_filepath, labels=self.labels, batch_size=30)
        pre_process_params = {
            'frame_splicing': 1,
            'features': 64,
            'window_size': 0.02,
            'n_fft': 512,
            'dither': 1e-05,
            'window': 'hann',
            'sample_rate': 16000,
            'normalize': 'per_feature',
            'window_stride': 0.01,
        }
        preprocessing = nemo_asr.AudioToMelSpectrogramPreprocessor(**pre_process_params)
        jasper_encoder = nemo_asr.JasperEncoder(
            feat_in=quartz_model_definition['AudioToMelSpectrogramPreprocessor']['features'],
            **quartz_model_definition['JasperEncoder'],
        )
        jasper_decoder = nemo_asr.JasperDecoderForCTC(feat_in=1024, num_classes=len(self.labels))
        ctc_loss = nemo_asr.CTCLossNM(num_classes=len(self.labels))

        # DAG
        audio_signal, a_sig_length, transcript, transcript_len = dl()
        processed_signal, p_length = preprocessing(input_signal=audio_signal, length=a_sig_length)

        encoded, encoded_len = jasper_encoder(audio_signal=processed_signal, length=p_length)
        log_probs = jasper_decoder(encoder_output=encoded)
        loss = ctc_loss(
            log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len,
        )

        loss_list = []
        callback = nemo.core.SimpleLossLoggerCallback(
            tensors=[loss], print_func=partial(self.print_and_log_loss, loss_log_list=loss_list), step_freq=1
        )

        self.nf.train(
            [loss], callbacks=[callback], optimizer="sgd", optimization_params={"max_steps": 3, "lr": 0.001},
        )
        self.nf.reset_trainer()

        # Assert that training loss went down
        assert loss_list[-1] < loss_list[0]

    @pytest.mark.integration
    def test_contextnet_ctc_training(self):
        """Integtaion test that instantiates a small ContextNet model and tests training with the sample asr data.
        Training is run for 3 forward and backward steps and asserts that loss after 3 steps is smaller than the loss
        at the first step.
        Note: Training is done with batch gradient descent as opposed to stochastic gradient descent due to CTC loss
        Checks SE-block with fixed context size and global context, residual_mode='stride_add' and 'stride_last' flags
        """
        with open(os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/contextnet_32.yaml"))) as f:
            contextnet_model_definition = self.yaml.load(f)
        dl = nemo_asr.AudioToTextDataLayer(manifest_filepath=self.manifest_filepath, labels=self.labels, batch_size=30)
        pre_process_params = {
            'frame_splicing': 1,
            'features': 80,
            'window_size': 0.025,
            'n_fft': 512,
            'dither': 1e-05,
            'window': 'hann',
            'sample_rate': 16000,
            'normalize': 'per_feature',
            'window_stride': 0.01,
        }
        preprocessing = nemo_asr.AudioToMelSpectrogramPreprocessor(**pre_process_params)

        spec_aug = nemo_asr.SpectrogramAugmentation(**contextnet_model_definition['SpectrogramAugmentation'])

        contextnet_encoder = nemo_asr.ContextNetEncoder(
            feat_in=contextnet_model_definition['AudioToMelSpectrogramPreprocessor']['features'],
            **contextnet_model_definition['ContextNetEncoder'],
        )
        contextnet_decoder = nemo_asr.ContextNetDecoderForCTC(feat_in=32, hidden_size=16, num_classes=len(self.labels))
        ctc_loss = nemo_asr.CTCLossNM(num_classes=len(self.labels))

        # DAG
        audio_signal, a_sig_length, transcript, transcript_len = dl()
        processed_signal, p_length = preprocessing(input_signal=audio_signal, length=a_sig_length)

        processed_signal = spec_aug(input_spec=processed_signal)

        encoded, encoded_len = contextnet_encoder(audio_signal=processed_signal, length=p_length)
        log_probs = contextnet_decoder(encoder_output=encoded)
        loss = ctc_loss(
            log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len,
        )

        loss_list = []
        callback = nemo.core.SimpleLossLoggerCallback(
            tensors=[loss], print_func=partial(self.print_and_log_loss, loss_log_list=loss_list), step_freq=1
        )

        self.nf.train(
            [loss], callbacks=[callback], optimizer="sgd", optimization_params={"max_steps": 3, "lr": 0.001},
        )
        self.nf.reset_trainer()

        # Assert that training loss went down
        assert loss_list[-1] < loss_list[0]

    @pytest.mark.integration
    def test_stft_conv_training(self):
        """Integtaion test that instantiates a small Jasper model and tests training with the sample asr data.
        test_stft_conv_training tests the torch_stft path while test_jasper_training tests the torch.stft path inside
        of AudioToMelSpectrogramPreprocessor.
        Training is run for 3 forward and backward steps and asserts that loss after 3 steps is smaller than the loss
        at the first step.
        Note: Training is done with batch gradient descent as opposed to stochastic gradient descent due to CTC loss
        """
        with open(os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/jasper_smaller.yaml"))) as file:
            jasper_model_definition = self.yaml.load(file)
        dl = nemo_asr.AudioToTextDataLayer(manifest_filepath=self.manifest_filepath, labels=self.labels, batch_size=30)
        pre_process_params = {
            'frame_splicing': 1,
            'features': 64,
            'window_size': 0.02,
            'n_fft': 512,
            'dither': 1e-05,
            'window': 'hann',
            'sample_rate': 16000,
            'normalize': 'per_feature',
            'window_stride': 0.01,
            'stft_conv': True,
        }
        preprocessing = nemo_asr.AudioToMelSpectrogramPreprocessor(**pre_process_params)
        jasper_encoder = nemo_asr.JasperEncoder(
            feat_in=jasper_model_definition['AudioToMelSpectrogramPreprocessor']['features'],
            **jasper_model_definition['JasperEncoder'],
        )
        jasper_decoder = nemo_asr.JasperDecoderForCTC(feat_in=1024, num_classes=len(self.labels))

        ctc_loss = nemo_asr.CTCLossNM(num_classes=len(self.labels))

        # DAG
        audio_signal, a_sig_length, transcript, transcript_len = dl()
        processed_signal, p_length = preprocessing(input_signal=audio_signal, length=a_sig_length)

        encoded, encoded_len = jasper_encoder(audio_signal=processed_signal, length=p_length)
        # logging.info(jasper_encoder)
        log_probs = jasper_decoder(encoder_output=encoded)
        loss = ctc_loss(
            log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len,
        )

        loss_list = []
        callback = nemo.core.SimpleLossLoggerCallback(
            tensors=[loss], print_func=partial(self.print_and_log_loss, loss_log_list=loss_list), step_freq=1
        )

        self.nf.train(
            [loss], callbacks=[callback], optimizer="sgd", optimization_params={"max_steps": 3, "lr": 0.001},
        )
        self.nf.reset_trainer()

        # Assert that training loss went down
        assert loss_list[-1] < loss_list[0]

    @pytest.mark.integration
    @pytest.mark.skipduringci
    def test_jasper_evaluation(self):
        """Integration test that tests EvaluatorCallback and NeuralModuleFactory.eval(). This test is skipped during
        CI as it is redundant with the Jenkins Jasper ASR CI tests.
        """
        # Note this test still has no asserts, but rather checks that the current eval path works
        with open(os.path.abspath(os.path.join(os.path.dirname(__file__), "../data/jasper_smaller.yaml"))) as file:
            jasper_model_definition = self.yaml.load(file)
        dl = nemo_asr.AudioToTextDataLayer(manifest_filepath=self.manifest_filepath, labels=self.labels, batch_size=8)
        pre_process_params = {
            'frame_splicing': 1,
            'features': 64,
            'window_size': 0.02,
            'n_fft': 512,
            'dither': 1e-05,
            'window': 'hann',
            'sample_rate': 16000,
            'normalize': 'per_feature',
            'window_stride': 0.01,
        }
        preprocessing = nemo_asr.AudioToMelSpectrogramPreprocessor(**pre_process_params)
        jasper_encoder = nemo_asr.JasperEncoder(
            feat_in=jasper_model_definition['AudioToMelSpectrogramPreprocessor']['features'],
            **jasper_model_definition['JasperEncoder'],
        )
        jasper_decoder = nemo_asr.JasperDecoderForCTC(feat_in=1024, num_classes=len(self.labels))
        ctc_loss = nemo_asr.CTCLossNM(num_classes=len(self.labels))
        greedy_decoder = nemo_asr.GreedyCTCDecoder()
        # DAG
        audio_signal, a_sig_length, transcript, transcript_len = dl()
        processed_signal, p_length = preprocessing(input_signal=audio_signal, length=a_sig_length)

        encoded, encoded_len = jasper_encoder(audio_signal=processed_signal, length=p_length)
        # logging.info(jasper_encoder)
        log_probs = jasper_decoder(encoder_output=encoded)
        loss = ctc_loss(
            log_probs=log_probs, targets=transcript, input_length=encoded_len, target_length=transcript_len,
        )
        predictions = greedy_decoder(log_probs=log_probs)

        from nemo.collections.asr.helpers import (
            process_evaluation_batch,
            process_evaluation_epoch,
        )

        eval_callback = nemo.core.EvaluatorCallback(
            eval_tensors=[loss, predictions, transcript, transcript_len],
            user_iter_callback=lambda x, y: process_evaluation_batch(x, y, labels=self.labels),
            user_epochs_done_callback=process_evaluation_epoch,
        )
        # Instantiate an optimizer to perform `train` action
        self.nf.eval(callbacks=[eval_callback])

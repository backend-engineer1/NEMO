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

import pytest
from omegaconf import DictConfig, OmegaConf, open_dict

import nemo.collections.asr as nemo_asr
from nemo.collections.asr.models import EncDecCTCModel, configs
from nemo.utils.config_utils import update_model_config


@pytest.fixture()
def asr_model():
    preprocessor = {'_target_': 'nemo.collections.asr.modules.AudioToMelSpectrogramPreprocessor'}
    encoder = {
        '_target_': 'nemo.collections.asr.modules.ConvASREncoder',
        'feat_in': 64,
        'activation': 'relu',
        'conv_mask': True,
        'jasper': [
            {
                'filters': 1024,
                'repeat': 1,
                'kernel': [1],
                'stride': [1],
                'dilation': [1],
                'dropout': 0.0,
                'residual': False,
                'separable': True,
                'se': True,
                'se_context_size': -1,
            }
        ],
    }

    decoder = {
        '_target_': 'nemo.collections.asr.modules.ConvASRDecoder',
        'feat_in': 1024,
        'num_classes': 28,
        'vocabulary': [
            ' ',
            'a',
            'b',
            'c',
            'd',
            'e',
            'f',
            'g',
            'h',
            'i',
            'j',
            'k',
            'l',
            'm',
            'n',
            'o',
            'p',
            'q',
            'r',
            's',
            't',
            'u',
            'v',
            'w',
            'x',
            'y',
            'z',
            "'",
        ],
    }
    modelConfig = DictConfig(
        {'preprocessor': DictConfig(preprocessor), 'encoder': DictConfig(encoder), 'decoder': DictConfig(decoder)}
    )

    model_instance = EncDecCTCModel(cfg=modelConfig)
    return model_instance


class TestEncDecCTCModel:
    @pytest.mark.unit
    def test_constructor(self, asr_model):
        asr_model.train()
        # TODO: make proper config and assert correct number of weights
        # Check to/from config_dict:
        confdict = asr_model.to_config_dict()
        instance2 = EncDecCTCModel.from_config_dict(confdict)
        assert isinstance(instance2, EncDecCTCModel)

    @pytest.mark.unit
    def test_vocab_change(self, asr_model):
        old_vocab = copy.deepcopy(asr_model.decoder.vocabulary)
        nw1 = asr_model.num_weights
        asr_model.change_vocabulary(new_vocabulary=old_vocab)
        # No change
        assert nw1 == asr_model.num_weights
        new_vocab = copy.deepcopy(old_vocab)
        new_vocab.append('!')
        new_vocab.append('$')
        new_vocab.append('@')
        asr_model.change_vocabulary(new_vocabulary=new_vocab)
        # fully connected + bias
        assert asr_model.num_weights == nw1 + 3 * (asr_model.decoder._feat_in + 1)

    @pytest.mark.unit
    def test_dataclass_instantiation(self, asr_model):
        model_cfg = configs.EncDecCTCModelConfig()

        # Update mandatory values
        vocabulary = asr_model.decoder.vocabulary
        model_cfg.model.labels = vocabulary

        # Update encoder
        model_cfg.model.encoder.activation = 'relu'
        model_cfg.model.encoder.feat_in = 64
        model_cfg.model.encoder.jasper = [
            nemo_asr.modules.conv_asr.JasperEncoderConfig(
                filters=1024,
                repeat=1,
                kernel=[1],
                stride=[1],
                dilation=[1],
                dropout=0.0,
                residual=False,
                se=True,
                se_context_size=-1,
            )
        ]

        # Update decoder
        model_cfg.model.decoder.feat_in = 1024
        model_cfg.model.decoder.num_classes = 28
        model_cfg.model.decoder.vocabulary = vocabulary

        # Construct the model
        asr_cfg = OmegaConf.create({'model': asr_model.cfg})
        model_cfg_v1 = update_model_config(model_cfg, asr_cfg)
        new_model = EncDecCTCModel(cfg=model_cfg_v1.model)

        assert new_model.num_weights == asr_model.num_weights
        # trainer and exp manager should be there
        assert 'trainer' in model_cfg_v1
        assert 'exp_manager' in model_cfg_v1
        # datasets and optim/sched should not be there after ModelPT.update_model_dataclass()
        assert 'train_ds' not in model_cfg_v1.model
        assert 'validation_ds' not in model_cfg_v1.model
        assert 'test_ds' not in model_cfg_v1.model
        assert 'optim' not in model_cfg_v1.model

        # Construct the model, without dropping additional keys
        asr_cfg = OmegaConf.create({'model': asr_model.cfg})
        model_cfg_v2 = update_model_config(model_cfg, asr_cfg, drop_missing_subconfigs=False)

        # Assert all components are in config
        assert 'trainer' in model_cfg_v2
        assert 'exp_manager' in model_cfg_v2
        assert 'train_ds' in model_cfg_v2.model
        assert 'validation_ds' in model_cfg_v2.model
        assert 'test_ds' in model_cfg_v2.model
        assert 'optim' in model_cfg_v2.model

        # Remove extra components (optim and sched can be kept without issue)
        with open_dict(model_cfg_v2.model):
            model_cfg_v2.model.pop('train_ds')
            model_cfg_v2.model.pop('validation_ds')
            model_cfg_v2.model.pop('test_ds')

        new_model = EncDecCTCModel(cfg=model_cfg_v2.model)

        assert new_model.num_weights == asr_model.num_weights
        # trainer and exp manager should be there

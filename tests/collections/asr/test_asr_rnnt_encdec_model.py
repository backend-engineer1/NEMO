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
from omegaconf import DictConfig, ListConfig

from nemo.collections.asr.models import EncDecRNNTModel

try:
    from warprnnt_pytorch import RNNTLoss

    WARP_RNNT_AVAILABLE = True

except (ImportError, ModuleNotFoundError):
    WARP_RNNT_AVAILABLE = False


@pytest.fixture()
def asr_model():
    preprocessor = {'cls': 'nemo.collections.asr.modules.AudioToMelSpectrogramPreprocessor', 'params': dict({})}

    # fmt: off
    labels = [' ', 'a', 'b', 'c', 'd', 'e', 'f',
              'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o',
              'p', 'q', 'r', 's', 't', 'u', 'v', 'w',
              'x', 'y', 'z', "'",
              ]
    # fmt: on

    model_defaults = {'enc_hidden': 1024, 'pred_hidden': 64}

    encoder = {
        'cls': 'nemo.collections.asr.modules.ConvASREncoder',
        'params': {
            'feat_in': 64,
            'activation': 'relu',
            'conv_mask': True,
            'jasper': [
                {
                    'filters': model_defaults['enc_hidden'],
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
        },
    }

    decoder = {
        '_target_': 'nemo.collections.asr.modules.RNNTDecoder',
        'prednet': {'pred_hidden': model_defaults['pred_hidden'], 'pred_rnn_layers': 1},
    }

    joint = {
        '_target_': 'nemo.collections.asr.modules.RNNTJoint',
        'jointnet': {'joint_hidden': 32, 'activation': 'relu'},
    }

    decoding = {'strategy': 'greedy_batch', 'greedy': {'max_symbols': 30}}

    modelConfig = DictConfig(
        {
            'labels': ListConfig(labels),
            'preprocessor': DictConfig(preprocessor),
            'model_defaults': DictConfig(model_defaults),
            'encoder': DictConfig(encoder),
            'decoder': DictConfig(decoder),
            'joint': DictConfig(joint),
            'decoding': DictConfig(decoding),
        }
    )

    model_instance = EncDecRNNTModel(cfg=modelConfig)
    return model_instance


class TestEncDecRNNTModel:
    @pytest.mark.skipif(
        not WARP_RNNT_AVAILABLE,
        reason='RNNTLoss has not been compiled. Please compile and install '
        'RNNT Loss first before running this test',
    )
    @pytest.mark.unit
    def test_constructor(self, asr_model):
        asr_model.train()
        # TODO: make proper config and assert correct number of weights
        # Check to/from config_dict:
        confdict = asr_model.to_config_dict()
        instance2 = EncDecRNNTModel.from_config_dict(confdict)
        assert isinstance(instance2, EncDecRNNTModel)

    @pytest.mark.skipif(
        not WARP_RNNT_AVAILABLE,
        reason='RNNTLoss has not been compiled. Please compile and install '
        'RNNT Loss first before running this test',
    )
    @pytest.mark.unit
    def test_vocab_change(self, asr_model):
        old_vocab = copy.deepcopy(asr_model.joint.vocabulary)
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
        # rnn embedding + joint + bias
        pred_embedding = 3 * (asr_model.decoder.pred_hidden)
        joint_joint = 3 * (asr_model.joint.joint_hidden + 1)
        assert asr_model.num_weights == (nw1 + (pred_embedding + joint_joint))

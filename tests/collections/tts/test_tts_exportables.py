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
import os
import tempfile

import onnx
import pytest
from omegaconf import DictConfig, ListConfig, OmegaConf

from nemo.collections.tts.models import FastPitchModel, HifiGanModel, TalkNetSpectModel, WaveGlowModel


@pytest.fixture()
def fastpitch_model():
    test_root = os.path.dirname(os.path.abspath(__file__))
    conf = OmegaConf.load(os.path.join(test_root, '../../../examples/tts/conf/fastpitch.yaml'))
    conf.train_dataset = conf.validation_datasets = '.'
    conf.model.train_ds = conf.model.test_ds = conf.model.validation_ds = None
    model = FastPitchModel(cfg=conf.model)
    return model


@pytest.fixture()
def hifigan_model():
    test_root = os.path.dirname(os.path.abspath(__file__))
    model = HifiGanModel.from_pretrained(model_name="tts_hifigan")
    return model


class TestExportable:
    @pytest.mark.run_only_on('GPU')
    @pytest.mark.unit
    @pytest.mark.skip('Fastpitch export PR pending')
    def test_FastPitchModel_export_to_onnx(self, fastpitch_model):
        model = fastpitch_model.cuda()
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, 'fp.onnx')
            model.export(output=filename, verbose=True, check_trace=True)

    @pytest.mark.run_only_on('GPU')
    @pytest.mark.unit
    def test_HifiGanModel_export_to_onnx(self, hifigan_model):
        model = hifigan_model.cuda()
        assert hifigan_model.generator is not None
        with tempfile.TemporaryDirectory() as tmpdir:
            filename = os.path.join(tmpdir, 'hfg.pt')
            model.export(output=filename, verbose=True, check_trace=True)


if __name__ == "__main__":
    t = TestExportable()
    t.test_FastPitchModel_export_to_onnx(fastpitch_model())

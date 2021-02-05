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
import shutil
import tempfile
from typing import Dict, Optional, Set, Union

import pytest
import torch
from omegaconf import DictConfig, OmegaConf, open_dict

from nemo.collections.asr.models import EncDecCTCModel, EncDecCTCModelBPE
from nemo.collections.nlp.models import PunctuationCapitalizationModel
from nemo.core.classes import ModelPT


def getattr2(object, attr):
    if not '.' in attr:
        return getattr(object, attr)
    else:
        arr = attr.split('.')
        return getattr2(getattr(object, arr[0]), '.'.join(arr[1:]))


class MockModel(ModelPT):
    def __init__(self, cfg, trainer=None):
        super(MockModel, self).__init__(cfg=cfg, trainer=trainer)
        self.w = torch.nn.Linear(10, 1)

        # mock temp file
        if 'temp_file' in self.cfg and self.cfg.temp_file is not None:
            self.temp_file = self.register_artifact('temp_file', self.cfg.temp_file)
            with open(self.temp_file, 'r') as f:
                self.temp_data = f.readlines()
        else:
            self.temp_file = None
            self.temp_data = None

    def forward(self, x):
        y = self.w(x)
        return y, self.temp_file

    def setup_training_data(self, train_data_config: Union[DictConfig, Dict]):
        self._train_dl = None

    def setup_validation_data(self, val_data_config: Union[DictConfig, Dict]):
        self._validation_dl = None

    def setup_test_data(self, test_data_config: Union[DictConfig, Dict]):
        self._test_dl = None

    def list_available_models(cls):
        return []


def _mock_model_config():
    conf = {'temp_file': None}
    conf = OmegaConf.create({'model': conf})
    OmegaConf.set_struct(conf, True)
    return conf


class TestSaveRestore:
    def __test_restore_elsewhere(
        self,
        model: ModelPT,
        attr_for_eq_check: Set[str] = None,
        override_config_path: Optional[Union[str, DictConfig]] = None,
        map_location: Optional[torch.device] = None,
        strict: bool = False,
        return_config: bool = False,
    ):
        """Test's logic:
            1. Save model into temporary folder (save_folder)
            2. Copy .nemo file from save_folder to restore_folder
            3. Delete save_folder
            4. Attempt to restore from .nemo file in restore_folder and compare to original instance
        """
        # Create a new temporary directory
        with tempfile.TemporaryDirectory() as restore_folder:
            with tempfile.TemporaryDirectory() as save_folder:
                save_folder_path = save_folder
                # Where model will be saved
                model_save_path = os.path.join(save_folder, f"{model.__class__.__name__}.nemo")
                model.save_to(save_path=model_save_path)
                # Where model will be restored from
                model_restore_path = os.path.join(restore_folder, f"{model.__class__.__name__}.nemo")
                shutil.copy(model_save_path, model_restore_path)
            # at this point save_folder should not exist
            assert save_folder_path is not None and not os.path.exists(save_folder_path)
            assert not os.path.exists(model_save_path)
            assert os.path.exists(model_restore_path)
            # attempt to restore
            model_copy = model.__class__.restore_from(
                restore_path=model_restore_path,
                map_location=map_location,
                strict=strict,
                return_config=return_config,
                override_config_path=override_config_path,
            )

            if return_config:
                return model_copy

            assert model.num_weights == model_copy.num_weights
            if attr_for_eq_check is not None and len(attr_for_eq_check) > 0:
                for attr in attr_for_eq_check:
                    assert getattr2(model, attr) == getattr2(model_copy, attr)

            return model_copy

    @pytest.mark.unit
    def test_EncDecCTCModel(self):
        # TODO: Switch to using named configs because here we don't really care about weights
        qn = EncDecCTCModel.from_pretrained(model_name="QuartzNet15x5Base-En")
        self.__test_restore_elsewhere(model=qn, attr_for_eq_check=set(["decoder._feat_in", "decoder._num_classes"]))

    @pytest.mark.unit
    def test_EncDecCTCModelBPE(self):
        # TODO: Switch to using named configs because here we don't really care about weights
        cn = EncDecCTCModelBPE.from_pretrained(model_name="ContextNet-192-WPE-1024-8x-Stride")
        self.__test_restore_elsewhere(model=cn, attr_for_eq_check=set(["decoder._feat_in", "decoder._num_classes"]))

    @pytest.mark.unit
    def test_EncDecCTCModelBPE(self):
        # TODO: Switch to using named configs because here we don't really care about weights
        cn = EncDecCTCModelBPE.from_pretrained(model_name="ContextNet-192-WPE-1024-8x-Stride")
        self.__test_restore_elsewhere(model=cn, attr_for_eq_check=set(["decoder._feat_in", "decoder._num_classes"]))

    @pytest.mark.unit
    def test_PunctuationCapitalization(self):
        # TODO: Switch to using named configs because here we don't really care about weights
        pn = PunctuationCapitalizationModel.from_pretrained(model_name='Punctuation_Capitalization_with_DistilBERT')
        self.__test_restore_elsewhere(
            model=pn, attr_for_eq_check=set(["punct_classifier.log_softmax", "punct_classifier.log_softmax"])
        )

    @pytest.mark.unit
    def test_mock_save_to_restore_from(self):
        with tempfile.NamedTemporaryFile('w') as empty_file:
            # Write some data
            empty_file.writelines(["*****\n"])
            empty_file.flush()

            # Update config
            cfg = _mock_model_config()
            cfg.model.temp_file = empty_file.name

            # Create model
            model = MockModel(cfg=cfg.model, trainer=None)
            model = model.to('cpu')

            assert model.temp_file == empty_file.name

            # Save test
            model_copy = self.__test_restore_elsewhere(model, map_location='cpu')

        # Restore test
        diff = model.w.weight - model_copy.w.weight
        assert diff.mean() <= 1e-9
        assert os.path.basename(model.temp_file) == model_copy.temp_file
        assert model_copy.temp_data == ["*****\n"]

    @pytest.mark.unit
    def test_mock_restore_from_config_only(self):
        with tempfile.NamedTemporaryFile('w') as empty_file:
            # Write some data
            empty_file.writelines(["*****\n"])
            empty_file.flush()

            # Update config
            cfg = _mock_model_config()
            cfg.model.temp_file = empty_file.name

            # Inject arbitrary config arguments (after creating model)
            with open_dict(cfg.model):
                cfg.model.xyz = "abc"

            # Create model
            model = MockModel(cfg=cfg.model, trainer=None)
            model = model.to('cpu')

            assert model.temp_file == empty_file.name

            # Save test
            model_config_copy = self.__test_restore_elsewhere(model, map_location='cpu', return_config=True)

        # Restore test
        assert isinstance(model_config_copy, DictConfig)
        assert model.cfg.temp_file == model_config_copy.temp_file
        assert model.cfg.xyz == model_config_copy.xyz

    @pytest.mark.unit
    def test_mock_restore_from_config_override_with_OmegaConf(self):
        with tempfile.NamedTemporaryFile('w') as empty_file:
            # Write some data
            empty_file.writelines(["*****\n"])
            empty_file.flush()

            # Update config
            cfg = _mock_model_config()
            cfg.model.temp_file = empty_file.name

            # Create model
            model = MockModel(cfg=cfg.model, trainer=None)
            model = model.to('cpu')

            assert model.temp_file == empty_file.name

            # Inject arbitrary config arguments (after creating model)
            with open_dict(cfg.model):
                cfg.model.xyz = "abc"

            # Save test (with overriden config as OmegaConf object)
            model_copy = self.__test_restore_elsewhere(model, map_location='cpu', override_config_path=cfg)

        # Restore test
        diff = model.w.weight - model_copy.w.weight
        assert diff.mean() <= 1e-9
        assert os.path.basename(model.temp_file) == model_copy.temp_file
        assert model_copy.temp_data == ["*****\n"]

        # Test that new config has arbitrary content
        assert model_copy.cfg.xyz == "abc"

    @pytest.mark.unit
    def test_mock_restore_from_config_override_with_yaml(self):
        with tempfile.NamedTemporaryFile('w') as empty_file, tempfile.NamedTemporaryFile('w') as config_file:
            # Write some data
            empty_file.writelines(["*****\n"])
            empty_file.flush()

            # Update config
            cfg = _mock_model_config()
            cfg.model.temp_file = empty_file.name

            # Create model
            model = MockModel(cfg=cfg.model, trainer=None)
            model = model.to('cpu')

            assert model.temp_file == empty_file.name

            # Inject arbitrary config arguments (after creating model)
            with open_dict(cfg.model):
                cfg.model.xyz = "abc"

            # Write new config into file
            OmegaConf.save(cfg, config_file)

            # Save test (with overriden config as OmegaConf object)
            model_copy = self.__test_restore_elsewhere(
                model, map_location='cpu', override_config_path=config_file.name
            )

            # Restore test
            diff = model.w.weight - model_copy.w.weight
            assert diff.mean() <= 1e-9
            assert os.path.basename(model.temp_file) == model_copy.temp_file
            assert model_copy.temp_data == ["*****\n"]

            # Test that new config has arbitrary content
            assert model_copy.cfg.xyz == "abc"

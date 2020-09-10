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

import pytorch_lightning as pl
from omegaconf.listconfig import ListConfig
from pytorch_lightning import seed_everything

from nemo.collections.asr.models import EncDecSpeakerLabelModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager

"""
Basic run (on GPU for 10 epochs):
EXP_NAME=sample_run
python ./speaker_reco.py --config-path='conf' --config-name='SpeakerNet_recognition_3x2x512.yaml' \
    trainer.max_epochs=10  \
    model.train_ds.batch_size=64 model.validation_ds.batch_size=64 \
    trainer.gpus=1 \
    model.decoder.params.num_classes=2 \
    exp_manager.name=$EXP_NAME +exp_manager.use_datetime_version=False \
    exp_manager.exp_dir='./speaker_exps'

Add PyTorch Lightning Trainer arguments from CLI:
    python speaker_reco.py \
        ... \
        +trainer.fast_dev_run=true

"""

seed_everything(42)


@hydra_runner(config_path="conf", config_name="SpeakerNet_recognition_3x2x512.yaml")
def main(cfg):

    logging.info(f'Hydra config: {cfg.pretty()}')
    trainer = pl.Trainer(**cfg.trainer)
    log_dir = exp_manager(trainer, cfg.get("exp_manager", None))
    speaker_model = EncDecSpeakerLabelModel(cfg=cfg.model, trainer=trainer)
    trainer.fit(speaker_model)
    model_path = os.path.join(log_dir, '..', 'spkr.nemo')
    speaker_model.save_to(model_path)

    if hasattr(cfg.model, 'test_ds') and cfg.model.test_ds.manifest_filepath is not None:
        if (isinstance(cfg.trainer.gpus, ListConfig) and len(cfg.trainer.gpus) > 1) or (
            isinstance(cfg.trainer.gpus, (int, str)) and int(cfg.trainer.gpus) > 1
        ):
            logging.info("Testing on single GPU to minimize DDP issues")
            trainer.gpus = 1

        trainer.test(speaker_model)


if __name__ == '__main__':
    main()

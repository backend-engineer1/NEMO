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

import pytorch_lightning as pl
from omegaconf import DictConfig

from nemo.collections.nlp.models import PunctuationCapitalizationModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="conf", config_name="punctuation_capitalization_config")
def main(cfg: DictConfig) -> None:
    logging.info(f'Config: {cfg.pretty()}')
    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))
    model = PunctuationCapitalizationModel(cfg.model, trainer=trainer)
    trainer.fit(model)
    if cfg.model.nemo_path:
        model.save_to(cfg.model.nemo_path)

    # run an inference on a few examples
    queries = [
        'we bought four shirts and one mug from the nvidia gear store in santa clara',
        'what can i do for you today',
        'how are you',
        'how is the weather in',
    ]
    inference_results = model.add_punctuation_capitalization(queries)

    for query, result in zip(queries, inference_results):
        logging.info(f'Query : {query}')
        logging.info(f'Result: {result.strip()}\n')


if __name__ == '__main__':
    main()

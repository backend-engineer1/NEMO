# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
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
from omegaconf import DictConfig

from nemo.collections.nlp.models import PunctuationCapitalizationModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


"""
This script shows how to perform evaluation and runs inference of a few examples.

More details on the task and data format could be found in tutorials/nlp/Punctuation_and_Capitalization.ipynb

*** Setting the configs ***

This script uses the `/examples/nlp/token_classification/conf/punctuation_capitalization_config.yaml` config file
by default. You may update the config file from the file directly. 
The other option is to set another config file via command line arguments by `--config-name=CONFIG_FILE_PATH'.

For more details about the config files and different ways of model restoration, see tutorials/00_NeMo_Primer.ipynb


*** Model Evaluation ***

    python punctuation_capitalization_evaluate.py \
    model.dataset.data_dir=<PATH_TO_DATA_DIR>  \
    pretrained_model=Punctuation_Capitalization_with_BERT_base_uncased 

<PATH_TO_DATA_DIR> - a directory that contains test_ds.text_file and test_ds.labels_file (see the config)
pretrained_model   - pretrained PunctuationCapitalizationModel model from list_available_models() or 
                     path to a .nemo file, for example: Punctuation_Capitalization_with_BERT_base_uncased or your_model.nemo

"""


@hydra_runner(config_path="conf", config_name="punctuation_capitalization_config")
def main(cfg: DictConfig) -> None:
    logging.info(
        'During evaluation/testing, it is currently advisable to construct a new Trainer with single GPU and \
            no DDP to obtain accurate results'
    )

    if not hasattr(cfg.model, 'test_ds'):
        raise ValueError(f'model.test_ds was not found in the config, skipping evaluation')
    else:
        gpu = 1 if cfg.trainer.gpus != 0 else 0

    trainer = pl.Trainer(
        gpus=gpu,
        precision=cfg.trainer.precision,
        amp_level=cfg.trainer.amp_level,
        logger=False,
        checkpoint_callback=False,
    )
    exp_dir = exp_manager(trainer, cfg.exp_manager)

    if not cfg.pretrained_model:
        raise ValueError(
            'To run evaluation and inference script a pre-trained model or .nemo file must be provided.'
            f'Choose from {PunctuationCapitalizationModel.list_available_models()} or "pretrained_model"="your_model.nemo"'
        )

    if os.path.exists(cfg.pretrained_model):
        model = PunctuationCapitalizationModel.restore_from(cfg.pretrained_model)
    elif cfg.pretrained_model in PunctuationCapitalizationModel.get_available_model_names():
        model = PunctuationCapitalizationModel.from_pretrained(cfg.pretrained_model)
    else:
        raise ValueError(
            f'Provide path to the pre-trained .nemo file or choose from {PunctuationCapitalizationModel.list_available_models()}'
        )

    data_dir = cfg.model.dataset.get('data_dir', None)
    if not data_dir:
        raise ValueError(
            'Specify a valid dataset directory that contains test_ds.text_file and test_ds.labels_file \
            with "model.dataset.data_dir" argument'
        )

    if not os.path.exists(data_dir):
        raise ValueError(f'{data_dir} is not found at')

    model.update_data_dir(data_dir=data_dir)
    model._cfg.dataset.use_cache = False

    if not hasattr(cfg.model, 'test_ds'):
        raise ValueError(f'model.test_ds was not found in the config, skipping evaluation')
    else:
        if model.prepare_test(trainer):
            model.setup_test_data()
            trainer.test(model)
        else:
            raise ValueError('Terminating evaluation')

    # run an inference on a few examples
    queries = [
        'we bought four shirts one pen and a mug from the nvidia gear store in santa clara',
        'what can i do for you today',
        'how are you',
    ]
    inference_results = model.add_punctuation_capitalization(queries)

    for query, result in zip(queries, inference_results):
        logging.info(f'Query : {query}')
        logging.info(f'Result: {result.strip()}\n')

    logging.info(f'Results are saved at {exp_dir}')


if __name__ == '__main__':
    main()

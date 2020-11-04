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
from omegaconf import DictConfig, OmegaConf

from nemo.collections.nlp.models import IntentSlotClassificationModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="conf", config_name="intent_slot_classification_config")
def main(cfg: DictConfig) -> None:
    logging.info(f'Config Params:\n {OmegaConf.to_yaml(cfg)}')
    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))

    model = IntentSlotClassificationModel(cfg.model, trainer=trainer)
    trainer.fit(model)
    logging.info('Training finished!')
    logging.info("================================================================================================")

    if cfg.model.nemo_path:
        model.save_to(cfg.model.nemo_path)
        logging.info(f'The model is saved into the `.nemo` file: {cfg.model.nemo_path}')

    # after model training is done, you can load the model from the saved checkpoint
    # and evaluate it on a data file or on given queries.
    logging.info("================================================================================================")
    logging.info("Starting the testing of the trained model on test set...")
    logging.info("We will load the latest model saved checkpoint from the training...")

    # run this only on the master thread in case of multi-gpu training, for single gpu no difference
    if trainer.checkpoint_callback is not None:
        # retrieve the path to the last checkpoint of the training in .nemo format
        checkpoint_path = os.path.join(
            trainer.checkpoint_callback.dirpath, trainer.checkpoint_callback.prefix + ".nemo"
        )

        # load a model from the checkpoint
        eval_model = IntentSlotClassificationModel.restore_from(restore_path=checkpoint_path)

        # we will setup testing data reusing the same config (test section)
        eval_model.setup_test_data(test_data_config=cfg.model.test_ds)

        # we will reinitialize the trainer with a single GPU and no distributed backend for the evaluation
        # we need to call the next line also to overcome an issue with a trainer reinitialization in PT
        cfg.trainer.gpus = 1 if cfg.trainer.gpus != 0 else 0
        cfg.trainer.accelerator = None
        eval_trainer = pl.Trainer(**cfg.trainer)

        eval_trainer.test(model=eval_model, ckpt_path=None, verbose=False)

        logging.info("Testing finished!")
        logging.info(
            "================================================================================================"
        )

        # run an inference on a few examples
        logging.info("======================================================================================")
        logging.info("Evaluate the model on given queries...")

        # this will work well if you train the model on Assistant dataset
        # for your own dataset change the examples appropriately
        queries = [
            'set alarm for seven thirty am',
            'lower volume by fifty percent',
            'what is my schedule for tomorrow',
        ]

        pred_intents, pred_slots = eval_model.predict_from_examples(queries)

        logging.info('The prediction results of some sample queries with the trained model:')
        for query, intent, slots in zip(queries, pred_intents, pred_slots):
            logging.info(f'Query : {query}')
            logging.info(f'Predicted Intent: {intent}')
            logging.info(f'Predicted Slots: {slots}')

        logging.info("Inference finished!")
        logging.info("==========================================================================================")


if __name__ == '__main__':
    main()

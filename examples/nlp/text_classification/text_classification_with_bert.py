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

"""
This script contains an example on how to train, evaluate and perform inference with the TextClassificationModel.
TextClassificationModel in NeMo supports text classification problems such as sentiment analysis or
domain/intent detection for dialogue systems, as long as the data follows the format specified below.

***Data format***
TextClassificationModel requires the data to be stored in TAB separated files (.tsv) with two columns of sentence and
label. Each line of the data file contains text sequences, where words are separated with spaces and label separated
with [TAB], i.e.:

[WORD][SPACE][WORD][SPACE][WORD][TAB][LABEL]

For example:

hide new secretions from the parental units[TAB]0
that loves its characters and communicates something rather beautiful about human nature[TAB]1
...

If your dataset is stored in another format, you need to convert it to this format to use the TextClassificationModel.


***Setting the configs***
The model and the PT trainer are defined in a config file which declares multiple important sections.
The most important ones are:
    model: All arguments that are related to the Model - language model, tokenizer, head classifier, optimizer,
            schedulers, and datasets/data loaders.
    trainer: Any argument to be passed to PyTorch Lightning including number of epochs, number of GPUs,
            precision level, etc.

This script uses the `/examples/nlp/text_classification/conf/text_classification_config.yaml` default config file
by default. You may update the config file from the file directly or by using the command line arguments.
Other option is to set another config file via command line arguments by `--config-name=CONFIG_FILE_PATH'.

You first need to set the num_classes in the config file which specifies the number of classes in the dataset.
Notice that some config lines, including `model.dataset.classes_num`, have `???` as their value, this means that values
for these fields are required to be specified by the user. We need to specify and set the `model.train_ds.file_name`,
`model.validation_ds.file_name`, and `model.test_ds.file_name` in the config file to the paths of the train, validation,
 and test files if they exist. We may do it by updating the config file or by setting them from the command line.


***How to run the script?***
For example the following would train a model for 50 epochs in 2 GPUs on a classification task with 2 classes:

# python text_classification_with_bert.py
        model.dataset.num_classes=2
        model.train_ds=PATH_TO_TRAIN_FILE
        model.validation_ds=PATH_TO_VAL_FILE
        trainer.max_epochs=50
        trainer.gpus=2

This script would also reload the last checkpoint after the training is done and does evaluation on the dev set,
then performs inference on some sample queries.

By default, this script uses examples/nlp/text_classification/conf/text_classifciation_config.py config file, and
you may update all the params in the config file from the command line. You may also use another config file like this:

# python text_classification_with_bert.py --config-name==PATH_TO_CONFIG_FILE
        model.dataset.num_classes=2
        model.train_ds=PATH_TO_TRAIN_FILE
        model.validation_ds=PATH_TO_VAL_FILE
        trainer.max_epochs=50
        trainer.gpus=2

"""
import os

import pytorch_lightning as pl
import torch
from omegaconf import DictConfig

from nemo.collections.nlp.models.text_classification import TextClassificationModel
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="conf", config_name="text_classification_config")
def main(cfg: DictConfig) -> None:
    logging.info(f'\nConfig Params:\n{cfg.pretty()}')
    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))

    if not cfg.model.train_ds.file_path:
        raise ValueError("'train_ds.file_path' need to be set for the training!")

    model = TextClassificationModel(cfg.model, trainer=trainer)
    logging.info("===========================================================================================")
    logging.info('Starting training...')
    trainer.fit(model)
    logging.info('Training finished!')
    logging.info("===========================================================================================")

    if cfg.model.nemo_path:
        # ,nemo file contains the last checkpoint and the params to initialize the model
        model.save_to(cfg.model.nemo_path)
        logging.info(f'Model is saved into `.nemo` file: {cfg.model.nemo_path}')
        checkpoint_path = cfg.model.nemo_path
    else:
        checkpoint_path = None

    # We evaluate the trained model on the test set if test_ds is set in the config file
    if cfg.model.test_ds.file_path:
        logging.info("===========================================================================================")
        logging.info("Starting the testing of the trained model on test set...")
        # The latest checkpoint would be used, set ckpt_path to 'best' to use the best one
        trainer.test(model=model, ckpt_path=None, verbose=False)
        logging.info("Testing finished!")
        logging.info("===========================================================================================")

    if checkpoint_path and os.path.exists(checkpoint_path):
        # You may create a model from a saved chechpoint and use the model.infer() method to
        # perform inference on a list of queries. There is no need of any trainer for inference.
        logging.info("===========================================================================================")
        logging.info("Starting the inference on some sample queries...")
        queries = [
            'by the end of no such thing the audience , like beatrice , has a watchful affection for the monster .',
            'director rob marshall went out gunning to make a great one .',
            'uneasy mishmash of styles and genres .',
        ]

        # use the path of the last checkpoint from the training, you may update it to any other checkpoints
        infer_model = TextClassificationModel.restore_from(restore_path=checkpoint_path)

        # move the model to the desired device for inference
        # we move the model to "cuda" if available otherwise "cpu" would be used
        if torch.cuda.is_available():
            infer_model.to("cuda")
        else:
            infer_model.to("cpu")

        # max_seq_length=512 is the maximum length BERT supports.
        results = infer_model.classifytext(queries=queries, batch_size=16, max_seq_length=512)

        logging.info('The prediction results of some sample queries with the trained model:')
        for query, result in zip(queries, results):
            logging.info(f'Query : {query}')
            logging.info(f'Predicted label: {result}')

        logging.info("Inference finished!")
        logging.info("===========================================================================================")
    else:
        logging.info("Inference is skipped as no checkpoint was found from the training!")


if __name__ == '__main__':
    main()

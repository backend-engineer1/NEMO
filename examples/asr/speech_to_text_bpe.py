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
# Preparing the Tokenizer for the dataset
Use the `process_asr_text_tokenizer.py` script under <NEMO_ROOT>/scripts in order to prepare the tokenizer.

```sh
python <NEMO_ROOT>/scripts/process_asr_text_tokenizer.py \
        --manifest=<path to train manifest files, seperated by commas> \
        --data_root="<output directory>" \
        --vocab_size=<number of tokens in vocabulary> \
        --tokenizer=<"bpe" or "wpe"> \
        --log
```

# Training the model
```sh
python speech_to_text_bpe.py \
    # (Optional: --config-path=<path to dir of configs> --config-name=<name of config without .yaml>) \
    model.train_ds.manifest_filepath=<path to train manifest> \
    model.validation_ds.manifest_filepath=<path to val/test manifest> \
    model.tokenizer.dir=<path to directory of tokenizer (not full path to the vocab file!)> \
    model.tokenizer.type=<either bpe or wpe> \
    trainer.gpus=2 \
    trainer.distributed_backend="ddp" \
    trainer.max_epochs=100 \
    model.optim.name="adamw" \
    model.optim.lr=0.1 \
    model.optim.betas=[0.9,0.999] \
    model.optim.weight_decay=0.0001 \
    model.optim.sched.warmup_ratio=0.05 \
    exp_manager.create_wandb_logger=True \
    exp_manager.wandb_logger_kwargs.name="AN4-BPE-1024" \
    exp_manager.wandb_logger_kwargs.project="AN4_BPE_1024"
```
"""
import pytorch_lightning as pl

from nemo.collections.asr.models.ctc_bpe_models import EncDecCTCModelBPE
from nemo.core.config import hydra_runner
from nemo.utils import logging
from nemo.utils.exp_manager import exp_manager


@hydra_runner(config_path="experimental/configs/", config_name="config_bpe")
def main(cfg):
    logging.info(f'Hydra config: {cfg.pretty()}')
    trainer = pl.Trainer(**cfg.trainer)
    exp_manager(trainer, cfg.get("exp_manager", None))
    asr_model = EncDecCTCModelBPE(cfg=cfg.model, trainer=trainer)

    trainer.fit(asr_model)


if __name__ == '__main__':
    main()

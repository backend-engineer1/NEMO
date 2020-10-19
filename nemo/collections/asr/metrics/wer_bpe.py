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

from typing import List

import editdistance
import torch
from pytorch_lightning.metrics import Metric

from nemo.collections.common.tokenizers.tokenizer_spec import TokenizerSpec
from nemo.utils import logging


class WERBPE(Metric):
    """
    This metric computes numerator and denominator for Overall Word Error Rate for BPE tokens (WER-BPE) between prediction and reference texts.
    When doing distributed training/evaluation the result of res=WERBPE(predictions, targets, target_lengths) calls
    will be all-reduced between all workers using SUM operations.
    Here contains two numbers res=[wer_numerator, wer_denominator]. WERBPE=wer_numerator/wer_denominator.

    If used with PytorchLightning LightningModule, include wer_numerator and wer_denominators inside validation_step results.
    Then aggregate (sum) then at the end of validation epoch to correctly compute validation WER.

    Example:
       def validation_step(self, batch, batch_idx):
           ...
           wer_num, wer_denom = self.__wer(predictions, transcript, transcript_len)
           return {'val_loss': loss_value, 'val_wer_num': wer_num, 'val_wer_denom': wer_denom}

       def validation_epoch_end(self, outputs):
           ...
           wer_num = torch.stack([x['val_wer_num'] for x in outputs]).sum()
           wer_denom = torch.stack([x['val_wer_denom'] for x in outputs]).sum()
           tensorboard_logs = {'validation_loss': val_loss_mean, 'validation_avg_wer': wer_num / wer_denom}
           return {'val_loss': val_loss_mean, 'log': tensorboard_logs}

    Args:
       vocabulary: NeMo tokenizer object, which inherits from TokenizerSpec.
       batch_dim_index: Index of the batch dimension.
       use_cer: Whether to compute word-error-rate or character-error-rate.
       ctc_decode: Whether to perform CTC decode.
       log_prediction: Whether to log a single decoded sample per call.

    Returns:
       res: a torch.Tensor object with two elements: [wer_numerator, wer_denominators]. To correctly compute average
       text word error rate, compute wer=wer_numerator/wer_denominators
    """

    def __init__(
        self,
        tokenizer: TokenizerSpec,
        batch_dim_index=0,
        use_cer=False,
        ctc_decode=True,
        log_prediction=True,
        dist_sync_on_step=False,
    ):
        super().__init__(dist_sync_on_step=dist_sync_on_step)
        self.tokenizer = tokenizer
        self.batch_dim_index = batch_dim_index
        self.blank_id = tokenizer.tokenizer.vocab_size
        self.use_cer = use_cer
        self.ctc_decode = ctc_decode
        self.log_prediction = log_prediction

        self.add_state("scores", default=torch.tensor(0), dist_reduce_fx='sum')
        self.add_state("words", default=torch.tensor(0), dist_reduce_fx='sum')

    def ctc_decoder_predictions_tensor(self, predictions: torch.Tensor) -> List[str]:
        """
        Decodes a sequence of labels to words
        """
        hypotheses = []
        # Drop predictions to CPU
        prediction_cpu_tensor = predictions.long().cpu()
        # iterate over batch
        for ind in range(prediction_cpu_tensor.shape[self.batch_dim_index]):
            prediction = prediction_cpu_tensor[ind].detach().numpy().tolist()
            # CTC decoding procedure
            decoded_prediction = []
            previous = self.blank_id
            for p in prediction:
                if (p != previous or previous == self.blank_id) and p != self.blank_id:
                    decoded_prediction.append(p)
                previous = p
            hypothesis = self.tokenizer.ids_to_text(decoded_prediction)
            hypotheses.append(hypothesis)
        return hypotheses

    def update(self, predictions: torch.Tensor, targets: torch.Tensor, target_lengths: torch.Tensor):
        words = 0.0
        scores = 0.0
        references = []
        with torch.no_grad():
            # prediction_cpu_tensor = tensors[0].long().cpu()
            targets_cpu_tensor = targets.long().cpu()
            tgt_lenths_cpu_tensor = target_lengths.long().cpu()

            # iterate over batch
            for ind in range(targets_cpu_tensor.shape[self.batch_dim_index]):
                tgt_len = tgt_lenths_cpu_tensor[ind].item()
                target = targets_cpu_tensor[ind][:tgt_len].numpy().tolist()
                reference = self.tokenizer.ids_to_text(target)
                references.append(reference)
            if self.ctc_decode:
                hypotheses = self.ctc_decoder_predictions_tensor(predictions)
            else:
                raise NotImplementedError("Implement me if you need non-CTC decode on predictions")

        if self.log_prediction:
            logging.info(f"\n")
            logging.info(f"reference:{references[0]}")
            logging.info(f"decoded  :{hypotheses[0]}")

        for h, r in zip(hypotheses, references):
            if self.use_cer:
                h_list = list(h)
                r_list = list(r)
            else:
                h_list = h.split()
                r_list = r.split()
            words += len(r_list)
            # Compute Levenstein's distance
            scores += editdistance.eval(h_list, r_list)

        self.scores = torch.tensor(scores).to(predictions.device)
        self.words = torch.tensor(words).to(predictions.device)
        # return torch.tensor([scores, words]).to(predictions.device)

    def compute(self):
        return self.scores / self.words

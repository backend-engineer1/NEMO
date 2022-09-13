# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

import itertools
from typing import Any, List

import torch
from omegaconf import OmegaConf
from omegaconf.dictconfig import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.data.language_modeling.megatron.t5_prompt_learning_dataset import (
    T5PromptLearningDataset,
    T5Sentinel,
)
from nemo.collections.nlp.models.language_modeling.megatron_base_prompt_learning_model import (
    MegatronBasePromptLearningModel,
)
from nemo.collections.nlp.models.language_modeling.megatron_t5_model import MegatronT5Model
from nemo.collections.nlp.modules.common.megatron.utils import average_losses_across_data_parallel_group
from nemo.collections.nlp.parts.nlp_overrides import NLPSaveRestoreConnector
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.utils import logging

try:
    from apex.transformer import parallel_state
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_no_pipelining import forward_backward_no_pipelining

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


__all__ = ['MegatronT5PromptLearningModel']


class MegatronT5PromptLearningModel(MegatronBasePromptLearningModel):
    """
    Model class for prompt-tuning or p-tuning a pretrained Megatron T5 model. 

    Prompt Tuning initalizes virtual prompt embeddings directly from a copy of
    certain token embeddings from the the pretrained T5 model's vocabulary
    and directly tunes these embedding weights. The token embeddings used in 
    initalization are specified by the user in the config file. The model can 
    be prompt-tuned for multiple tasks at once. Virtual prompts are stored in a 
    prompt table and can be added or deleted without disrupting virtual prompts 
    for other tasks. 

    P-tuning initializes an LSTM encoder model that generates virtual prompt
    embeddings for every task. Each task shares the same encoder. After p-tuning
    is compelete, the learned virtual prompts can be saved to the prompt table
    using add_ptuned_prompts_to_prompt_table(). Thus, if a user wants to add a 
    new virtual prompt via p-tuning, they do not need to retrain on all previous 
    tasks. This gives p-tuning the same task flexiblity as prompt-tuning.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        super().__init__(cfg, trainer)

    def forward(
        self, input_ids, dec_input, enc_mask, dec_mask, position_ids, taskname_ids, labels=None, inference=False,
    ):
        """
        Special forward method for p-tuning/prompt-tuning pretrained
        T5 style models.
        """
        # Get embeddings for text tokens and insert virtual token embeddings
        if inference:
            input_embeds = self.embed_input_inference(input_ids, taskname_ids)
        else:
            input_embeds = self.embed_input_train(input_ids, taskname_ids)

        # TODO: This check needs to be revisited with PP support.
        if hasattr(self.frozen_model.enc_dec_model.encoder_embedding, 'position_embeddings'):
            position_embeddings = self.frozen_model.enc_dec_model.encoder_embedding.position_embeddings(position_ids)
            encoder_input = input_embeds + position_embeddings
        else:
            encoder_input = input_embeds

        # Call forward on T5 model with preprocessed embeddings
        if self.autocast_dtype == torch.float32:
            output = self.frozen_model.enc_dec_model(
                enc_input_ids=None,
                enc_attn_mask=enc_mask,
                dec_input_ids=dec_input,
                dec_attn_mask=dec_mask,
                token_type_ids=None,
                labels=labels,
                output_enc_hidden_only=False,
                enc_input=encoder_input,
            )
        else:
            with torch.autocast(device_type="cuda", dtype=self.autocast_dtype):
                output = self.frozen_model.enc_dec_model(
                    enc_input_ids=None,
                    enc_attn_mask=enc_mask,
                    dec_input_ids=dec_input,
                    dec_attn_mask=dec_mask,
                    token_type_ids=None,
                    labels=labels,
                    output_enc_hidden_only=False,
                    enc_input=encoder_input,
                )

        return output, encoder_input

    def load_frozen_model(self, cfg, trainer):
        self.megatron_amp_o2 = cfg.get('megatron_amp_O2', False)

        # TODO: Fix this once apex patches FusedScaledMaskedSoftmax.
        # This is a workaround for the fact that `masked_softmax_fusion` has issues with certain input sizes that may be present while finetuning.
        t5_cfg = MegatronT5Model.restore_from(cfg.get('language_model_path'), trainer=trainer, return_config=True)
        OmegaConf.set_struct(t5_cfg, True)
        with open_dict(t5_cfg):
            if hasattr(t5_cfg, 'encoder') and hasattr(t5_cfg, 'decoder'):
                t5_cfg.encoder.masked_softmax_fusion = False
                t5_cfg.decoder.masked_softmax_fusion = False
            else:
                t5_cfg.masked_softmax_fusion = False
            t5_cfg.megatron_amp_O2 = self.megatron_amp_o2
            # hack to make the _GLOBAL_NUM_MICROBATCHES_CALCULATOR initialize
            t5_cfg.micro_batch_size = cfg.get('micro_batch_size', 4)
            t5_cfg.global_batch_size = cfg.get('global_batch_size', 4)
            t5_cfg.precision = trainer.precision

        self.frozen_model = MegatronT5Model.restore_from(
            cfg.get('language_model_path'),
            trainer=trainer,
            override_config_path=t5_cfg,
            save_restore_connector=NLPSaveRestoreConnector(),
        )

        # Freeze all T5 model weights for prompt-tuning/p-tuning
        self.frozen_model.freeze()

    def fwd_bwd_step(self, batch, batch_idx, forward_only):
        """
            Dataloader produces a global batch which is turned into a list of microbatches.
            The list of microbatches is then piped through the pipeline using Apex fwd/bwd functions.
        """
        # Get seq length of batch
        _, seq_length = batch[0].shape
        tensor_shape = [seq_length, self.cfg.micro_batch_size, self.hidden_size]

        if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
            raise Exception("Pipeline parallelism is not supported yet")
        else:
            losses_reduced_per_micro_batch = forward_backward_no_pipelining(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch,
                model=self,
                forward_only=forward_only,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
                disable_autocast=True,
                grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
            )

        # only the last stages of the pipeline return losses
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.concat(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            # we're not on the last pipeline stage so no losses
            loss_mean = torch.tensor(0.0).cuda()

        return loss_mean

    def get_forward_output_and_loss_func(self):
        def fwd_output_and_loss_func(batch, model):
            batch = [x.cuda(non_blocking=True) for x in batch]
            enc_input, dec_input, labels, loss_mask, enc_mask, dec_mask, position_ids, taskname_ids = batch

            output_tensor, encoder_input = model(
                enc_input, dec_input, enc_mask, dec_mask, position_ids, taskname_ids, labels, inference=False
            )

            def loss_func(output_tensor):
                loss = self.frozen_model.loss_func(loss_mask, output_tensor)
                reduced_loss = average_losses_across_data_parallel_group([loss])
                return loss, {'avg': reduced_loss}

            return output_tensor, loss_func

        return fwd_output_and_loss_func

    def backward(self, *args, **kwargs):
        """ LightningModule hook to do backward.
            We want this to do nothing since we run backward in the fwd/bwd functions from apex.
            No need to call it here.
        """
        return

    def optimizer_zero_grad(self, *args, **kwargs):
        """ LightningModule hook to zero grad.
            We want this to do nothing as we are zeroing grads during the training_step.
        """
        return

    def training_step(self, batch, batch_idx):

        self._optimizer.zero_grad()
        loss_mean = self.fwd_bwd_step(batch, batch_idx, forward_only=False)
        self.allreduce_gradients()

        ## logging
        # we can only log on one rank if it is rank zero so we broadcast from last rank
        # we can avoid this broadcast by updating the PTL log function to accept specific ranks
        torch.distributed.broadcast(loss_mean, get_last_rank())

        if self.cfg.precision == 16:
            loss_scale = self.trainer.precision_plugin.scaler._scale
            if loss_scale is not None:
                self.log('loss_scale', loss_scale)

        self.log('reduced_train_loss', loss_mean, prog_bar=True, rank_zero_only=True)
        lr = self._optimizer.param_groups[0]['lr']
        self.log('lr', lr, rank_zero_only=True)
        self.log('global_step', self.trainer.global_step, prog_bar=True, rank_zero_only=True)

        return loss_mean

    def inference_step(self, batch, batch_idx, inference=False):
        enc_input, dec_input, labels, loss_mask, enc_mask, dec_mask, position_ids, taskname_ids = batch

        mode = self.training
        self.eval()

        input_embeds = self.embed_input_train(enc_input, taskname_ids)

        # TODO: This check needs to be revisited with PP support.
        if hasattr(self.frozen_model.enc_dec_model.encoder_embedding, 'position_embeddings'):
            position_embeddings = self.frozen_model.enc_dec_model.encoder_embedding.position_embeddings(position_ids)
            encoder_input = input_embeds + position_embeddings
        else:
            encoder_input = input_embeds

        loss_mean = self.fwd_bwd_step(batch, batch_idx, forward_only=True)

        predicted_token_ids, log_probs = self.frozen_model.decode(
            tokens_enc=enc_input,
            enc_mask=enc_mask,
            num_tokens_to_generate=self.decoder_seq_length,
            encoder_input=encoder_input,
        )

        processed_inputs, processed_preds, processed_labels = [], [], []
        preds = predicted_token_ids.cpu().numpy().tolist()
        labels = labels.cpu().numpy().tolist()
        enc_inputs = enc_input.cpu().numpy().tolist()

        for i, (enc_input, pred, label) in enumerate(zip(enc_inputs, preds, labels)):
            if self.tokenizer.eos_id in pred:
                idx = pred.index(self.tokenizer.eos_id)
                pred = pred[:idx]

            # Sentencepiece case
            if hasattr(self.tokenizer, 'special_token_to_id'):
                pred = [id for id in pred if id not in self.tokenizer.special_token_to_id.values()]
                label = [id for id in label if id not in self.tokenizer.special_token_to_id.values()]
                enc_input = [id for id in enc_input if id not in self.tokenizer.special_token_to_id.values()]
            # HF Autotokenizer case.
            else:
                pred = [id for id in pred if id not in self.tokenizer.tokenizer.additional_special_tokens_ids]
                label = [id for id in label if id not in self.tokenizer.tokenizer.additional_special_tokens_ids]
                enc_input = [
                    id for id in enc_input if id not in self.tokenizer.tokenizer.additional_special_tokens_ids
                ]

            pred = self.tokenizer.ids_to_text(pred)
            label = self.tokenizer.ids_to_text(label)
            enc_input = self.tokenizer.ids_to_text(enc_input)

            processed_preds.append(pred)
            processed_labels.append(label)
            processed_inputs.append(enc_input)

        self.train(mode=mode)
        return {
            'loss': loss_mean,
            'predicted_token_ids': processed_preds,
            'labels': processed_labels,
            'enc_inputs': processed_inputs,
        }

    def inference_epoch_end(self, outputs):

        gather_results = [None for _ in range(parallel_state.get_data_parallel_world_size())]

        all_preds = list(itertools.chain(*[item['predicted_token_ids'] for item in outputs]))
        all_labels = list(itertools.chain(*[item['labels'] for item in outputs]))
        all_inputs = list(itertools.chain(*[item['enc_inputs'] for item in outputs]))

        assert len(all_preds) == len(all_labels)
        assert len(all_preds) == len(all_inputs)

        # Gather inputs, preds, labels from all workers
        torch.distributed.all_gather_object(
            gather_results,
            [(input, pred, label) for (input, pred, label) in zip(all_inputs, all_preds, all_labels)],
            group=parallel_state.get_data_parallel_group(),
        )

        # Deduplicate sentences that may have been distributed across multiple data parallel ranks.
        if parallel_state.get_data_parallel_rank() == 0:

            gather_results_dedup = list(set(itertools.chain(*gather_results)))

            correct = 0
            for (input, pred, label) in gather_results_dedup:
                if pred == label:
                    correct += 1

            val_acc = correct / len(gather_results_dedup)
            val_acc = torch.tensor(val_acc).cuda()

            logging.info(f'Validation accuracy: {val_acc}')
        else:
            val_acc = torch.tensor(0.0).cuda()

        averaged_loss = torch.stack([item['loss'] for item in outputs]).mean()
        logging.info(f'Validation loss: {averaged_loss}')

        self.log('val_loss', averaged_loss, prog_bar=True, rank_zero_only=True)
        self.log('val_acc', val_acc, prog_bar=True, rank_zero_only=True)

    def validation_step(self, batch, batch_idx, inference=False):
        outcome = self.inference_step(batch, batch_idx, inference=inference)
        return outcome

    def validation_epoch_end(self, outputs):
        self.inference_epoch_end(outputs)

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        self.validation_epoch_end(outputs)

    def build_virtual_prompt_dataset(
        self, dataset_paths, batch_size, for_train, drop_last, shuffle, num_workers, pin_memory
    ):
        dataset = T5PromptLearningDataset(
            datasets=dataset_paths,
            tokenizer=self.tokenizer,
            virtual_prompt_source=self.virtual_prompt_source,
            task_templates=self.task_templates,
            pseudo_tokens=self.pseudo_tokens,
            pad_token_id=self.pad_token_id,
            max_seq_length=self.cfg.data.get('max_seq_length', self.frozen_model.cfg.max_position_embeddings),
            min_seq_length=self.cfg.data.get('min_seq_length', 1),
            add_bos=self.cfg.data.get('add_bos', False),
            add_eos=self.cfg.data.get('add_eos', True),
            for_train=for_train,
        )

        rank = parallel_state.get_data_parallel_rank()
        world_size = parallel_state.get_data_parallel_world_size()
        sampler = torch.utils.data.distributed.DistributedSampler(
            dataset, num_replicas=world_size, rank=rank, shuffle=shuffle
        )

        dataloader = torch.utils.data.DataLoader(
            dataset,
            collate_fn=dataset.collate_fn,
            sampler=sampler,
            batch_size=batch_size // world_size,
            drop_last=drop_last,
            num_workers=num_workers,
            pin_memory=pin_memory,
        )
        print('build success', len(dataloader), dataset_paths)
        return dataset, dataloader

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: int = 0) -> Any:

        enc_input, dec_input, labels, loss_mask, enc_mask, dec_mask, position_ids, taskname_ids = batch

        input_embeds = self.embed_input_inference(enc_input, taskname_ids)

        # TODO: This check needs to be revisited with PP support.
        if hasattr(self.frozen_model.enc_dec_model.encoder_embedding, 'position_embeddings'):
            position_embeddings = self.frozen_model.enc_dec_model.encoder_embedding.position_embeddings(position_ids)
            encoder_input = input_embeds + position_embeddings
        else:
            encoder_input = input_embeds

        predicted_token_ids, log_probs = self.frozen_model.decode(
            tokens_enc=enc_input,
            enc_mask=enc_mask,
            num_tokens_to_generate=self.decoder_seq_length,
            encoder_input=encoder_input,
        )

        processed_preds = []
        processed_labels = []
        processed_inputs = []

        preds = predicted_token_ids.cpu().numpy().tolist()
        enc_inputs = enc_input.cpu().numpy().tolist()

        if labels is not None:
            labels = labels.cpu().numpy().tolist()
        else:
            labels = [None] * len(preds)

        for i, (enc_input, pred, label) in enumerate(zip(enc_inputs, preds, labels)):
            if self.tokenizer.eos_id in pred:
                idx = pred.index(self.tokenizer.eos_id)
                pred = pred[:idx]

            pred = [
                id
                for id in pred
                if id not in self.tokenizer.tokenizer.additional_special_tokens_ids
                and id not in self.tokenizer.text_to_ids(T5Sentinel.FIRST.value)
            ]  # delete the sentinel token at the beginning of prediction

            pred = self.tokenizer.ids_to_text(pred)
            processed_preds.append(pred)

            enc_input = [
                id for id in enc_input if id not in self.tokenizer.text_to_ids(T5Sentinel.FIRST.value)
            ]  # delete the sentinel token added to the end of input

            input = self.tokenizer.ids_to_text(enc_input)
            processed_inputs.append(input)

            if label:
                label = [
                    id
                    for id in label
                    if id not in self.tokenizer.tokenizer.additional_special_tokens_ids
                    and id not in self.tokenizer.text_to_ids(T5Sentinel.FIRST.value)
                ]  # delete the sentinel token at the beginning of label

                label = self.tokenizer.ids_to_text(label)
            processed_labels.append(label)

        return {
            'enc_input': processed_inputs,
            'predicted_token_ids': processed_preds,
            'log_probs': log_probs,
            'labels': processed_labels,
        }

    def on_predict_epoch_end(self, outputs: List[Any]) -> None:

        gather_results = [None for _ in range(parallel_state.get_data_parallel_world_size())]
        all_preds = list(itertools.chain(*[item['predicted_token_ids'] for item in outputs[0]]))
        all_labels = list(itertools.chain(*[item['labels'] for item in outputs[0]]))
        all_inputs = list(itertools.chain(*[item['enc_input'] for item in outputs[0]]))

        assert len(all_preds) == len(all_labels)
        assert len(all_preds) == len(all_inputs)

        # Gather inputs, predictions, and ground truths from all workers
        torch.distributed.all_gather_object(
            gather_results,
            [(input, pred, label) for (input, pred, label) in zip(all_inputs, all_preds, all_labels)],
            group=parallel_state.get_data_parallel_group(),
        )

        # Deduplicate sentences that may have been distributed across multiple data parallel ranks.
        if parallel_state.get_data_parallel_rank() == 0:
            gather_results_dedup = list(set(itertools.chain(*gather_results)))

            input_prediction_pair = []
            correct = 0
            for (input, pred, label) in gather_results_dedup:
                input_prediction_pair.append((input, pred))
                if label:
                    if pred == label:
                        correct += 1

            acc = correct / len(gather_results_dedup) if all_labels[0] else None

            results = {'input_prediction_pair': input_prediction_pair, 'acc': acc}
            logging.info(f'Prediction results: {results}')
            logging.info(f'Test finish---------------------------------')

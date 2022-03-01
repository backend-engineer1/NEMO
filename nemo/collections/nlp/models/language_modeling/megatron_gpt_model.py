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
import re
from typing import Any, Dict, List, Optional, Union

import torch
import torch.nn.functional as F
from omegaconf.dictconfig import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.data.language_modeling.megatron.gpt_dataset import build_train_valid_test_datasets
from nemo.collections.nlp.data.language_modeling.megatron.gpt_prompt_tuning_dataset import GPTPromptTuningDataset
from nemo.collections.nlp.data.language_modeling.megatron.megatron_batch_samplers import (
    MegatronPretrainingBatchSampler,
    MegatronPretrainingRandomBatchSampler,
)
from nemo.collections.nlp.models.language_modeling.megatron.gpt_model import GPTModel
from nemo.collections.nlp.models.nlp_model import NLPModel
from nemo.collections.nlp.modules.common.megatron.clip_grads import clip_grad_norm_fp32
from nemo.collections.nlp.modules.common.megatron.megatron_init import initialize_model_parallel_for_nemo
from nemo.collections.nlp.modules.common.megatron.module import Float16Module
from nemo.collections.nlp.modules.common.megatron.utils import (
    average_losses_across_data_parallel_group,
    get_ltor_masks_and_position_ids,
)
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.collections.nlp.parts.nlp_overrides import GradScaler
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.core.optim import MainParamsOptimizerWrapper, prepare_lr_scheduler
from nemo.utils import AppState, logging

try:
    from apex.transformer import parallel_state, tensor_parallel
    from apex.contrib.layer_norm.layer_norm import FastLayerNorm
    from apex.normalization.fused_layer_norm import FusedLayerNorm  # NOQA
    from apex.transformer.pipeline_parallel.schedules.common import (
        build_model,
        listify_model,
    )
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_without_interleaving import (
        forward_backward_pipelining_without_interleaving,
    )
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_no_pipelining import forward_backward_no_pipelining
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches, _reconfigure_microbatch_calculator

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


def _get_params_for_weight_decay_optimization(
    model: Union[torch.nn.Module, List[torch.nn.Module]],
) -> Dict[str, torch.nn.Parameter]:
    """Divide params into with-weight-decay and without-weight-decay groups.

    Layernorms and biases will have no weight decay but the rest will.
    """
    modules = listify_model(model)
    weight_decay_params = {'params': []}
    no_weight_decay_params = {'params': [], 'weight_decay': 0.0}
    for module in modules:
        for module_ in module.modules():
            if isinstance(module_, (FusedLayerNorm, FastLayerNorm)):
                no_weight_decay_params['params'].extend(
                    [p for p in list(module_._parameters.values()) if p is not None]
                )
            else:
                weight_decay_params['params'].extend(
                    [p for n, p in list(module_._parameters.items()) if p is not None and n != 'bias']
                )
                no_weight_decay_params['params'].extend(
                    [p for n, p in list(module_._parameters.items()) if p is not None and n == 'bias']
                )

    return weight_decay_params, no_weight_decay_params


class MegatronGPTModel(NLPModel):
    """
    Megatron GPT pretraining and prompt tuning
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
        super().__init__(cfg, trainer=trainer)
        self.cfg = cfg

        self._validate_trainer()

        # used in NVIDIA NGC PyTorch containers
        self._enable_nvidia_optimizations()

        if self.cfg.get('use_cpu_initialization', False) is False:
            torch.cuda.set_device(trainer.local_rank)

        initialize_model_parallel_for_nemo(
            world_size=trainer.world_size,
            global_rank=trainer.global_rank,
            local_rank=trainer.local_rank,
            tensor_model_parallel_size=cfg.get('tensor_model_parallel_size', 1),
            pipeline_model_parallel_size=cfg.get('pipeline_model_parallel_size', 1),
            micro_batch_size=cfg.get('micro_batch_size'),
            global_batch_size=cfg.get('global_batch_size'),
            seed=self.cfg.get('seed', 1234),
            apex_transformer_log_level=self.cfg.get('apex_transformer_log_level', 30),
        )

        self.tokenizer = get_nmt_tokenizer(
            library=self.cfg.tokenizer.library,
            model_name=self.cfg.tokenizer.type,
            tokenizer_model=self.register_artifact("tokenizer_model", self.cfg.tokenizer.model),
            vocab_file=self.register_artifact("vocab_file", self.cfg.tokenizer.vocab_file),
            merges_file=self.register_artifact("merges_file", self.cfg.tokenizer.merge_file),
        )

        vocab_size = self.tokenizer.vocab_size

        self.padded_vocab_size = self._vocab_size_with_padding(
            orig_vocab_size=vocab_size,
            make_vocab_size_divisible_by=cfg.get('make_vocab_size_divisible_by', 128),
            tensor_model_parallel_size=cfg.get('tensor_model_parallel_size', 1),
        )

        # TODO: Not sure how to use lists of modules with PTL.
        # This means we can only use pipeline parallelism without the interleaved schedule.
        self.model = build_model(model_provider_func=self.model_provider_func, wrap_with_ddp=False)[0]

        # Prompt tuning initialization
        self.use_soft_prompts = self.cfg.get('use_soft_prompts', False)

        if self.use_soft_prompts:
            if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
                raise NotImplementedError("Prompt tuning is not yet supported for pipeline parallel > 1")

            self.prompts_to_tune = set([])
            self.prompt_table = set([])
            self.next_prompt_id = 0
            self.num_prompt_tokens = cfg.get('num_prompt_tokens', 100)

            if self.cfg.get('existing_prompt_tags', None):
                # Fill table with prev tuned prompt tags and their ids
                self.prompt_table = set(self.cfg.existing_prompt_tags)

                # Get max prompt id from table for starting point of new prompt ids
                self.next_prompt_id = max(self.prompt_table, key=lambda x: x[1])[1]

        self.setup_optimizer_param_groups()

        self.megatron_amp_o2 = cfg.get('megatron_amp_O2', False)

        if self.megatron_amp_o2:

            # Pre-allocate the model on GPU to have master parameters allocated on the same device with matching data type
            self.model.cuda(torch.cuda.current_device())

            # Model wrapper to convert both model and inputs to half precision
            self.model = Float16Module(module=self.model, precision=cfg.precision)

        if self.trainer.precision == 32:
            self.autocast_dtype = torch.float
        elif self.trainer.precision == 16:
            self.autocast_dtype = torch.half
        elif self.trainer.precision == 'bf16':
            self.autocast_dtype = torch.bfloat16
        else:
            raise ValueError('precision must be in [32, 16, "bf16"]')

    def model_provider_func(self, pre_process, post_process):
        """Model depends on pipeline paralellism."""
        model = GPTModel(
            vocab_size=self.padded_vocab_size,
            hidden_size=self.cfg.hidden_size,
            max_position_embeddings=self.cfg.max_position_embeddings,
            num_layers=self.cfg.num_layers,
            num_attention_heads=self.cfg.num_attention_heads,
            apply_query_key_layer_scaling=self.cfg.get('apply_query_key_layer_scaling', True),
            kv_channels=self.cfg.get('kv_channels', None),
            ffn_hidden_size=self.cfg.ffn_hidden_size,
            num_tokentypes=0,
            parallel_output=True,
            pre_process=pre_process,
            post_process=post_process,
            init_method_std=self.cfg.get('init_method_std', 0.02),
            fp16_lm_cross_entropy=self.cfg.get('fp16_lm_cross_entropy', False),
            use_cpu_initialization=self.cfg.get('use_cpu_initialization', False),
            hidden_dropout=self.cfg.get('hidden_dropout', 0.1),
            precision=self.cfg.get('precision', 16),
            fp32_residual_connection=self.cfg.get('fp32_residual_connection', False),
            activations_checkpoint_method=self.cfg.get('activations_checkpoint_method', None),
            activations_checkpoint_num_layers=self.cfg.get('activations_checkpoint_num_layers', 1),
            layernorm_epsilon=self.cfg.get('layernorm_epsilon', 1e-5),
            onnx_safe=self.cfg.get('onnx_safe', False),
            use_soft_prompts=self.cfg.get('use_soft_prompts', False),
            num_prompt_tokens=self.cfg.get('num_prompt_tokens', 100),
            existing_prompt_tags=self.cfg.get('existing_prompt_tags', None),
            persist_layer_norm=self.cfg.get('persist_layer_norm', False),
        )

        return model

    def forward(self, tokens, text_position_ids, attention_mask, labels, prompt_ids=None):
        output_tensor = self.model(tokens, text_position_ids, attention_mask, labels=labels, prompt_ids=prompt_ids,)
        return output_tensor

    def setup_optimizer_param_groups(self):
        """ModelPT override. Optimizer will get self._optimizer_param_groups"""
        self._optimizer_param_groups = _get_params_for_weight_decay_optimization([self.model])

    def training_step(self, batch, batch_idx):
        """
            Our dataloaders produce a micro-batch and then we fetch
            a number of microbatches depending on the global batch size and model parallel size
            from the dataloader to produce a list of microbatches.
            Batch should be a list of microbatches and those microbatches should on CPU.
            Microbatches are then moved to GPU during the pipeline.
            The list of microbatches is then piped through the pipeline using Apex fwd/bwd functions.
        """

        # we zero grads here because we also call backward in the apex fwd/bwd functions
        self._optimizer.zero_grad()

        if self.use_soft_prompts:
            # The micro batches are already prepared for apex by the prompt tuning dataclass
            batch_for_pipeline = batch
            tensor_shape = [len(batch_for_pipeline[0][0]), self.cfg.micro_batch_size, self.cfg.hidden_size]
        else:
            # we prepare the micro batches for the apex fwd/bwd function
            batch_for_pipeline = self.process_global_batch(batch)
            tensor_shape = [self.cfg.encoder_seq_length, self.cfg.micro_batch_size, self.cfg.hidden_size]

        if self.cfg.get('pipeline_model_parallel_size', 1) > 1:

            losses_reduced_per_micro_batch = forward_backward_pipelining_without_interleaving(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch_for_pipeline,
                model=self.model,
                forward_only=False,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
                grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
            )
        else:
            losses_reduced_per_micro_batch = forward_backward_no_pipelining(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch_for_pipeline,
                model=self.model,
                forward_only=False,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
                grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
            )

        # only the last stages of the pipeline return losses
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.concat(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            loss_mean = torch.tensor(0.0).cuda()

        # TODO: if we're not using pipeline, then we should do async allreduce (better perf)
        # in order to do this with O2, we need the async handler to be added to apex fwd/bwd function
        if self.megatron_amp_o2:
            # main grads are stored in the MainParamsOptimizer wrapper
            self._optimizer.allreduce_main_grads()  # @sangkug we think this is fine

            self.allreduce_first_last_embeddings()
        else:

            self.allreduce_gradients()  # @sangkug we think this is causing memory to blow up (hurts perf)

            self.allreduce_first_last_embeddings()

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
        # TODO: make sure compute_consumed_samples works for pipeline parallelism
        self.log(
            'consumed_samples',
            self.compute_consumed_samples(self.trainer.global_step - self.init_global_step),
            prog_bar=True,
            rank_zero_only=True,
        )

        return loss_mean

    def on_train_batch_end(self, outputs, batch, batch_idx: int, unused: Optional[int] = 0) -> None:
        super().on_train_batch_end(outputs, batch, batch_idx)

        # TODO: Replace with newer override for scheduler.step() instead of
        # search for plugins for fp16 GradScalar
        if self.trainer.precision_plugin is not None and isinstance(
            self.trainer.precision_plugin, NativeMixedPrecisionPlugin
        ):
            precision_plugin = self.trainer.precision_plugin

            if (
                hasattr(precision_plugin, 'scaler')
                and precision_plugin.scaler is not None
                and isinstance(precision_plugin.scaler, GradScaler)
            ):
                grad_scaler = precision_plugin.scaler

                # If the grad scaler skipped its optimizer step due to infs/nans,
                # decrement the step of all schedulers.
                if grad_scaler.optimizer_update_skipped is not None and grad_scaler.optimizer_update_skipped is True:
                    schedulers = self.trainer.lr_schedulers

                    if not schedulers or not self.trainer.lightning_module.automatic_optimization:
                        return

                    for scheduler in schedulers:
                        # Decrement the counter by 2, then perform a scheduler.step() to perform a no-up
                        # as well as update the optimizer lr in all param groups
                        scheduler['scheduler'].last_epoch -= 2
                        scheduler['scheduler'].step()

                    # Increase the max step count by 1
                    self.trainer.fit_loop.max_steps = self.trainer.fit_loop.max_steps + 1

                    # Reset the optimizer update skipped to `None` - this is to prevent scheduler no-ops during
                    # accumulated gradient updates.
                    grad_scaler.optimizer_update_skipped = None

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

    def allreduce_gradients(self):
        """Reduce gradients across data parallel ranks.
           Modified from megatron-lm: https://github.com/NVIDIA/Megatron-LM/blob/d41696840ed0a7edb7e0499eb82a48ae112d9bb3/megatron/model/distributed.py#L188
        """
        # Bucketize and all-reduce
        buckets = {}
        for param in self.parameters():
            if param.requires_grad and param.grad is not None:
                tp = param.data.type()
                if tp not in buckets:
                    buckets[tp] = []
                buckets[tp].append(param)
                # param.main_grad = param.grad

        # For each bucket, all-reduce and copy all-reduced grads.
        for tp in buckets:
            bucket = buckets[tp]
            grads = [param.grad.data for param in bucket]
            coalesced = torch._utils._flatten_dense_tensors(grads)
            coalesced /= parallel_state.get_data_parallel_world_size()
            torch.distributed.all_reduce(coalesced, group=parallel_state.get_data_parallel_group())
            for buf, synced in zip(grads, torch._utils._unflatten_dense_tensors(coalesced, grads)):
                buf.copy_(synced)

    def allreduce_first_last_embeddings(self):

        # Modified from megatron-lm: https://github.com/NVIDIA/Megatron-LM/blob/d41696840ed0a7edb7e0499eb82a48ae112d9bb3/megatron/training.py#L407
        # All-reduce word_embeddings' grad across first and last stages to ensure
        # that word_embeddings parameters stay in sync.
        # This should only run for models that support pipelined model parallelism
        # (BERT and GPT-2).
        if parallel_state.get_pipeline_model_parallel_world_size() > 1 and (
            parallel_state.is_pipeline_first_stage() or parallel_state.is_pipeline_last_stage()
        ):
            if self.model.share_word_embeddings:
                word_embeddings_weight = self.model.word_embeddings_weight()
                if self.megatron_amp_o2:
                    # O2 recipe stores a "main" copy of weights and grads
                    grad = word_embeddings_weight.main_grad
                else:
                    grad = word_embeddings_weight.grad
                torch.distributed.all_reduce(grad, group=parallel_state.get_embedding_group())

    def get_forward_output_and_loss_func(self):
        def fwd_output_and_loss_func(batch, model):
            batch = [x.cuda(non_blocking=True) for x in batch]

            if self.use_soft_prompts:
                tokens, labels, loss_mask, attention_mask, position_ids, prompt_ids = batch
                output_tensor = model(tokens, position_ids, attention_mask, labels, prompt_ids=prompt_ids)
            else:
                tokens, labels, loss_mask, attention_mask, position_ids = batch
                attention_mask = attention_mask[0:1]
                output_tensor = model(tokens, position_ids, attention_mask, labels)

            def loss_func(output_tensor):
                loss = self.loss_func(loss_mask, output_tensor)
                reduced_loss = average_losses_across_data_parallel_group([loss])
                return loss, {'avg': reduced_loss}

            return output_tensor, loss_func

        return fwd_output_and_loss_func

    def get_forward_output_only_func(self):
        def fwd_output_only_func(batch, model):
            batch = [x.cuda() for x in batch]

            if self.use_soft_prompts:
                tokens, attention_mask, position_ids, prompt_ids = batch
                output_tensor = model(tokens, position_ids, attention_mask, prompt_ids=prompt_ids)
            else:
                tokens, attention_mask, position_ids = batch
                attention_mask = attention_mask[0:1]
                output_tensor = model(tokens, position_ids, attention_mask)

            def id_func(output_tensor):
                return output_tensor, {'logits': output_tensor}

            return output_tensor, id_func

        return fwd_output_only_func

    def on_pretrain_routine_start(self) -> None:
        # keep a copy of init_global_step
        self.init_global_step = self.trainer.global_step
        return super().on_pretrain_routine_start()

    def validation_step(self, batch, batch_idx):
        """
            Our dataloaders produce a micro-batch and then we fetch
            a number of microbatches depending on the global batch size and model parallel size
            from the dataloader to produce a list of microbatches.
            The list of microbatches is then piped through the pipeline using Apex fwd/bwd functions.
        """

        if self.use_soft_prompts:
            # The micro batches are already prepared for apex by the prompt tuning dataclass
            batch_for_pipeline = batch
            tensor_shape = [len(batch_for_pipeline[0][0]), self.cfg.micro_batch_size, self.cfg.hidden_size]
        else:
            batch_for_pipeline = self.process_global_batch(batch)
            tensor_shape = [self.cfg.encoder_seq_length, self.cfg.micro_batch_size, self.cfg.hidden_size]

        if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
            losses_reduced_per_micro_batch = forward_backward_pipelining_without_interleaving(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch_for_pipeline,
                model=self.model,
                forward_only=True,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
            )
        else:
            losses_reduced_per_micro_batch = forward_backward_no_pipelining(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch_for_pipeline,
                model=self.model,
                forward_only=True,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
            )

        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.concat(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            # we're not on the last pipeline stage so no losses
            loss_mean = []

        return loss_mean

    def validation_epoch_end(self, outputs):
        if parallel_state.is_pipeline_last_stage():
            # only the last pipeline parallel stages return loss
            averaged_loss = torch.stack(outputs).mean()
        else:
            averaged_loss = torch.tensor(0.0).cuda()

        # we can only log on one rank if it is rank zero so we broadcast from last rank
        torch.distributed.broadcast(averaged_loss, get_last_rank())

        self.log('val_loss', averaged_loss, prog_bar=True, rank_zero_only=True)
        self.log(
            'consumed_samples',
            self.compute_consumed_samples(self.trainer.global_step - self.init_global_step),
            rank_zero_only=True,
        )

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        averaged_loss = average_losses_across_data_parallel_group(outputs)
        logging.info(f'test_loss: {averaged_loss[0]}')

    def loss_func(self, loss_mask, output_tensor):
        losses = output_tensor.float()
        loss_mask = loss_mask.view(-1).float()
        # TODO: add nemo version here
        loss = torch.sum(losses.view(-1) * loss_mask) / loss_mask.sum()  # sequence level nll
        return loss

    def process_global_batch(self, global_batch):
        """ Prepares the global batch for apex fwd/bwd functions.
            Global batch is a list of micro batches.
        """
        return [
            global_batch["tokens"],
            global_batch["labels"],
            global_batch["loss_mask"],
            global_batch["attention_mask"],
            global_batch["position_ids"],
        ]

    def build_train_valid_test_datasets(self):
        if self.use_soft_prompts:
            return

        logging.info('Building GPT datasets.')
        global_batch_size = self.trainer.world_size * self.cfg.micro_batch_size / self.cfg.tensor_model_parallel_size
        # Compute trianing micro-batch steps: total_global_batch_steps x grad_acumms_per_global_batch
        max_train_steps = self.trainer.max_steps * self.trainer.accumulate_grad_batches
        eval_iters = (max_train_steps // self.trainer.val_check_interval + 1) * self.trainer.limit_val_batches
        test_iters = self.trainer.limit_test_batches

        train_valid_test_num_samples = [
            max_train_steps * global_batch_size,
            eval_iters * global_batch_size,
            test_iters * global_batch_size,
        ]
        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            cfg=self.cfg,
            trainer=self.trainer,
            data_prefix=self.cfg.data.data_prefix,
            data_impl=self.cfg.data.data_impl,
            splits_string=self.cfg.data.splits_string,
            train_valid_test_num_samples=train_valid_test_num_samples,
            seq_length=self.cfg.data.seq_length,
            seed=self.cfg.seed,
            skip_warmup=self.cfg.data.get('skip_warmup', True),
            tokenizer=self.tokenizer,
        )
        if self._train_ds is not None:
            logging.info(f'Length of train dataset: {len(self._train_ds)}')
        if self._validation_ds is not None:
            logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        if self._test_ds is not None:
            logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building GPT datasets.')

        return self._train_ds, self._validation_ds, self._test_ds

    def build_pretraining_data_loader(self, dataset, consumed_samples):
        """Buld dataloader given an input dataset."""

        if dataset is None:
            return None

        logging.info(f'Building dataloader with consumed samples: {consumed_samples}')
        # Megatron sampler
        if hasattr(self.cfg.data, 'dataloader_type') and self.cfg.data.dataloader_type is not None:
            if self.cfg.data.dataloader_type == 'single':
                batch_sampler = MegatronPretrainingBatchSampler(
                    total_samples=len(dataset),
                    consumed_samples=consumed_samples,
                    num_micro_batch_times_micro_batch_size=self.cfg.global_batch_size
                    // parallel_state.get_data_parallel_world_size(),
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                )
            elif self.cfg.data.dataloader_type == 'cyclic':
                batch_sampler = MegatronPretrainingRandomBatchSampler(
                    total_samples=len(dataset),
                    consumed_samples=consumed_samples,
                    num_micro_batch_times_micro_batch_size=self.cfg.global_batch_size
                    // parallel_state.get_data_parallel_world_size(),
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                )
            else:
                raise ValueError('cfg.data.dataloader_type must be "single" or "cyclic"')
        else:
            raise ValueError('cfg.data.dataloader_type not found. Must be "single" or "cyclic"')

        return torch.utils.data.DataLoader(
            dataset, batch_sampler=batch_sampler, num_workers=self.cfg.data.num_workers, pin_memory=True,
        )

    def build_prompt_tuning_dataset(self, dataset_path):
        dataset = GPTPromptTuningDataset(
            dataset_path=dataset_path,
            tokenizer=self.tokenizer,
            prompt_table=self.prompt_table,
            num_prompt_tokens=self.cfg.num_prompt_tokens,
            micro_batch_size=self.cfg.micro_batch_size,
            max_seq_length=self.cfg.data.get('max_seq_length', self.cfg.max_position_embeddings),
            min_seq_length=self.cfg.data.get('min_seq_length', 1),
            add_bos=self.cfg.data.get('add_bos', False),
            add_eos=self.cfg.data.get('add_eos', True),
            calc_loss_on_answer_only=self.cfg.get('calc_loss_on_answer_only', False),
        )

        dataloader = torch.utils.data.DataLoader(
            dataset,
            batch_size=self.cfg.global_batch_size,
            collate_fn=dataset.collate_fn,
            num_workers=self.cfg.data.num_workers,
            drop_last=True,
            shuffle=True,
            pin_memory=True,
        )

        return dataset, dataloader

    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """
        resume_checkpoint_path = self.trainer.checkpoint_connector.resume_from_checkpoint_fit_path
        if resume_checkpoint_path:
            try:
                init_consumed_samples = int(
                    float(re.findall(r"consumed_samples\=([0-9]+.[0-9]+)", resume_checkpoint_path)[0])
                )
            except (ValueError, TypeError):
                logging.warning("Cannot parse the checkpoint file to get the consumed samples. assume it is zero.")
                init_consumed_samples = 0
        else:
            init_consumed_samples = 0
        self.init_consumed_samples = init_consumed_samples

        # Initalize soft prompts before loading datasets and training
        if self.use_soft_prompts:
            self.init_new_prompts()

        if stage == 'predict':
            return
        else:
            # TODO: consider adding a ModelPT guard to check if model is being restored.
            # allowing restored models to optionally setup datasets
            self.build_train_valid_test_datasets()
            self.setup_training_data(self.cfg.data)
            self.setup_validation_data(self.cfg.data)
            self.setup_test_data(self.cfg.data)

        # when using pipeline model parallel the final stage need to initialize word embeddings
        if parallel_state.get_pipeline_model_parallel_world_size() > 1:
            self.model.sync_initial_word_embeddings()

    def setup_training_data(self, cfg):
        if self.use_soft_prompts:
            if cfg.get('train_ds', None):
                self._train_ds, self._train_dl = self.build_prompt_tuning_dataset(self.cfg.data.train_ds)
            else:
                raise AttributeError('No prompt tuning train dataset was specified in the cfg file')

            # Freeze all weights except prompt embeddings and setup optimizer with prompt embedding params
            self.prompt_tuning_param_freeze_and_optimizer_setup()

        elif hasattr(self, '_train_ds'):
            consumed_samples = self.compute_consumed_samples(0)
            logging.info(
                f'Setting up train dataloader with len(len(self._train_ds)): {len(self._train_ds)} and consumed samples: {consumed_samples}'
            )
            self._train_dl = self.build_pretraining_data_loader(self._train_ds, consumed_samples)

    def setup_validation_data(self, cfg):
        if self.use_soft_prompts:
            if cfg.get('valid_ds', None):
                self._validation_ds, self._validation_dl = self.build_prompt_tuning_dataset(self.cfg.data.valid_ds)
            else:
                raise AttributeError('No prompt tuning validation dataset was specified in the cfg file')

        elif hasattr(self, '_validation_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up validation dataloader with len(len(self._validation_ds)): {len(self._validation_ds)} and consumed samples: {consumed_samples}'
            )
            self._validation_dl = self.build_pretraining_data_loader(self._validation_ds, consumed_samples)

    def setup_test_data(self, cfg):
        if self.use_soft_prompts:
            if cfg.get('test_ds', None):
                self._test_ds, self._test_dl = self.build_prompt_tuning_dataset(self.cfg.data.test_ds)
            else:
                logging.info('No prompt tuning test dataset file provided in config, skipping')

        elif hasattr(self, '_test_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up test dataloader with len(len(self._test_ds)): {len(self._test_ds)} and consumed samples: {consumed_samples}'
            )
            self._test_dl = self.build_pretraining_data_loader(self._test_ds, consumed_samples)

    def configure_optimizers(self):
        self.setup_optimization()

        # Wrap the baseline optimizer with the optimizer class with master parameters
        if self.megatron_amp_o2 and self._optimizer is not None:
            if self.cfg.precision == 'bf16':
                fp32_grad_accum = True
                contiguous_grad_bucket = True
            elif self.cfg.precision == 16:
                fp32_grad_accum = False
                # TODO: contiguous grad bucket for fp16 is also planned to be supported
                contiguous_grad_bucket = False

            # TODO: this should be true when not using pipeline parallelism
            # we will support that for bf16 when we have async handler from apex
            # and we will support it for fp16 when we have it implemented in the O2 recipe
            async_grad_allreduce = False

            self._optimizer = MainParamsOptimizerWrapper(
                self._optimizer,
                fp32_grad_accum=fp32_grad_accum,
                contiguous_grad_bucket=contiguous_grad_bucket,
                async_grad_allreduce=async_grad_allreduce,
            )
            assert self._trainer.max_steps is not None, "'max_steps' is missing in trainer config."
            sched_config = self._cfg.optim.sched
            sched_config['max_steps'] = self._trainer.max_steps
            self._scheduler = prepare_lr_scheduler(
                optimizer=self._optimizer, scheduler_config=sched_config, train_dataloader=self._train_dl
            )

        if self._scheduler is None:
            return self._optimizer
        else:
            return [self._optimizer], [self._scheduler]

    def compute_consumed_samples(self, steps_since_resume=0):
        app_state = AppState()
        consumed_samples = (
            self.init_consumed_samples
            + steps_since_resume * app_state.data_parallel_size * self.cfg.micro_batch_size * get_num_microbatches()
        )
        return int(consumed_samples)

    def configure_gradient_clipping(self, *args, **kwargs):
        """PTL hook to configure gradients.
           We use gradient clipping implementation from megatron-lm.
        """
        clip_val = self.trainer.gradient_clip_val
        if clip_val is None:
            return

        clip_val = float(clip_val)
        if clip_val <= 0:
            return

        if self.megatron_amp_o2:
            # grep fp32 master parameters for gradient clipping
            if self.use_soft_prompts:
                raise NotImplementedError("Prompt tuning is not implemented for amp_o2")
            parameters = self._optimizer.get_parameters()
        else:
            parameters = self.get_parameters()

        grad_norm = clip_grad_norm_fp32(parameters=parameters, max_norm=clip_val)

        self.log('grad_norm', grad_norm, rank_zero_only=True)

    def prompt_tuning_param_freeze_and_optimizer_setup(self):
        """Freeze weights of word embeddings and decoder, leaving only prompt embeddings unfrozen
        """
        weight_decay_params = {'params': []}
        no_weight_decay_params = {'params': [], 'weight_decay': 0.0}

        for param in self.model.parameters():
            param.requires_grad = False

        # Only want new prompt tags to be tunable, leave existing prompt tags alone
        for prompt_tag in self.model.language_model.prompt_table.prompt_table.keys():
            if prompt_tag in self.prompts_to_tune:
                for params in self.model.language_model.prompt_table.prompt_table[prompt_tag].parameters():
                    params.requires_grad = True
                    weight_decay_params['params'].append(params)
            else:
                for param in self.model.language_model.prompt_table.prompt_table[prompt_tag].parameters():
                    param.requires_grad = False

        self._optimizer_param_groups = weight_decay_params, no_weight_decay_params

    @classmethod
    def _bucketize_gpt_inference(cls, batch, use_soft_prompts=False):
        batch_tokens, lens, tokens_to_generate, compute_logprobs = batch[:4]
        batch_size = len(batch_tokens)
        tokens_to_generate = tokens_to_generate[0]
        batch_tokens = batch_tokens.tolist()

        if use_soft_prompts:
            prompt_tags = batch[4]

        # unpad tokens
        indxs = [index for index in range(batch_size)]
        for lenn, index in zip(lens, indxs):
            batch_tokens[index] = batch_tokens[index][:lenn]

        # chunk tokens by same length
        pre_buckets, lens = [], list(set(lens.tolist()))
        for lenn in lens:
            pre_buckets.append([(tokens, index) for index, tokens in enumerate(batch_tokens) if len(tokens) == lenn])

        buckets, positions, bucket_prompt_tags = [], [], []

        # get buckets and prompts initial positions
        for bucket in pre_buckets:
            buckets.append(torch.tensor([item[0] for item in bucket]).to(device='cuda'))
            positions.append([item[1] for item in bucket])

            # bucket prompt tags identically to their corresponding examples
            if use_soft_prompts:
                bucket_prompt_tags.append([prompt_tags[item[1]] for item in bucket])

        # Flatten position list
        positions = [item for sublist in positions for item in sublist]

        # Form request
        request = {"tokens": buckets, "prompt_tags": bucket_prompt_tags}

        return request, positions, tokens_to_generate, compute_logprobs[0]

    def get_parameters(self):
        params = []
        for param_group in self._optimizer_param_groups:
            for param in param_group['params']:
                params.append(param)
        return params

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: Optional[int] = None) -> Any:
        request, positions, tokens_to_generate, compute_logprobs = MegatronGPTModel._bucketize_gpt_inference(
            batch, self.use_soft_prompts
        )

        if compute_logprobs:
            response = self.compute_logprobs(request, positions)
        else:
            response = self.complete(request, positions, tokens_to_generate)

        return response

    def complete(self, request: Dict, positions: List, tokens_to_generate: int):
        """
            Autoregressively invokes language model in the inference mode
        Args:
            request:
                * tokens: List of "buckets" with unpadded tokens of the same length
                * prompt_tags: List of "buckets" where each bucket contains the prompt_tag strings
                               specifying the prompt tag to use (optional)
            positions: List with initial prompts positions
            tokens_to_generate: int value denoting amount of tokens model should generate

        Returns:
            response: A python list of tuples
                (text, tokens, log_probs, offsets)
                * text: string, inputted prompt + generated text by model
                * tokens: list of tokens correspond to text
                * log_probs: list of tokens log probabilities
                * offsets: list of tokens start positions in text

        """
        app_state = AppState()

        results = []
        request_tokens = request["tokens"]

        for idx, tokens in enumerate(request_tokens):
            micro_batch_size = tokens.shape[0]
            _reconfigure_microbatch_calculator(
                rank=app_state.global_rank,
                rampup_batch_size=None,
                global_batch_size=micro_batch_size,
                micro_batch_size=micro_batch_size,
                data_parallel_size=1,
            )

            # For prompt tuned GPT models
            if self.use_soft_prompts:
                if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
                    raise ValueError('complete method is not yet supported for pipeline with soft prompts')
                prompt_tags = request["prompt_tags"][idx]
                prompt_tags_to_ids = dict(self.prompt_table)
                prompt_ids = torch.tensor([prompt_tags_to_ids[tag] for tag in prompt_tags])
            else:
                prompt_ids = None

            logsoftmaxlayer = torch.nn.LogSoftmax(dim=-1)

            for i in range(tokens_to_generate + 1):
                if self.use_soft_prompts:
                    batch_size = len(tokens)
                    full_length = len(tokens[0]) + self.num_prompt_tokens

                    # Get postion ids for text after soft prompt
                    position_ids = torch.arange(
                        start=self.num_prompt_tokens, end=full_length, dtype=torch.long, device=self.device
                    )
                    position_ids = position_ids.unsqueeze(0).expand_as(tokens).clone()

                    # Make attention mask starting with first token in soft prompt
                    attention_mask = torch.tril(
                        torch.ones((batch_size, full_length, full_length), device=self.device)
                    ).view(batch_size, 1, full_length, full_length)
                    attention_mask = attention_mask < 0.5

                else:
                    attention_mask, _, position_ids = get_ltor_masks_and_position_ids(
                        data=tokens,
                        eod_token=self.tokenizer.eos_id,
                        reset_position_ids=self.cfg.get('reset_position_ids', False),
                        reset_attention_mask=self.cfg.get('reset_attention_mask', False),
                        eod_mask_loss=self.cfg.get('eod_mask_loss', False),
                    )
                attention_mask_repeat = torch.concat([attention_mask for _ in range(micro_batch_size)])
                if self.use_soft_prompts:
                    batch = [tokens, attention_mask_repeat, position_ids, prompt_ids]
                else:
                    batch = [tokens, attention_mask_repeat, position_ids]
                tensor_shape = [tokens.shape[1], micro_batch_size, self.cfg.hidden_size]
                if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
                    output_tensor = forward_backward_pipelining_without_interleaving(
                        forward_step_func=self.get_forward_output_only_func(),
                        batch=batch,
                        model=self.model,
                        forward_only=True,
                        tensor_shape=tensor_shape,
                        dtype=self.autocast_dtype,
                    )
                else:
                    output_tensor = forward_backward_no_pipelining(
                        forward_step_func=self.get_forward_output_only_func(),
                        batch=batch,
                        model=self.model,
                        forward_only=True,
                        tensor_shape=tensor_shape,
                        dtype=self.autocast_dtype,
                    )

                # get output tensor
                if parallel_state.is_pipeline_last_stage():
                    output_tensor = output_tensor[0]['logits']
                    output_tensor = tensor_parallel.gather_from_tensor_model_parallel_region(output_tensor)

                    log_probs, token_ids = torch.max(logsoftmaxlayer(output_tensor), dim=-1)
                    tokens = torch.cat([tokens, torch.unsqueeze(token_ids[:, -1], 1)], dim=1)
                else:
                    log_probs = torch.zeros((tokens.shape[0], tokens.shape[1]), dtype=torch.float).cuda()
                    tokens = torch.zeros((tokens.shape[0], tokens.shape[1] + 1), dtype=tokens.dtype).cuda()

                torch.distributed.broadcast(tokens, get_last_rank())
                torch.distributed.broadcast(log_probs, get_last_rank())

            # add to results as (text, tokens, log_probs, offsets)
            for token, prob in zip(tokens, log_probs.tolist()):
                results.append(
                    (self.tokenizer.ids_to_text(token[:-1]), self.tokenizer.ids_to_tokens(token[:-1]), prob, [0],)
                )

        # offsets calculation
        for item in results:
            for index, token in enumerate(item[1]):
                if index != len(item[1]) - 1:
                    item[3].append(len(token) + item[3][-1])

        # return prompts in the order that they were input
        response = [0 for i in range(len(positions))]
        for item, index in zip(results, positions):
            response[index] = item

        return response

    def compute_logprobs(self, request: Dict, positions: List):
        """
            Only logprobs computation without generation tokens
        Args:
            request:
                * tokens: List of "buckets" with unpadded tokens of the same length
                * prompt_tags: List of "buckets" where each bucket contains the prompt_tag strings
                                    specifying the prompt tag to use (optional)
            positions: List with initial prompts positions
        Returns:
            response: A python list of tuples
            (text, tokens, log_probs, offsets)
            * text: string, inputted prompt + generated text by model
            * tokens: list of tokens correspond to text
            * log_probs: list of log_softmax's from output_tensor in respect to text tokens
            * offsets: list of tokens start positions in text
        """
        app_state = AppState()

        results = []
        request_tokens = request["tokens"]
        for idx, tokens in enumerate(request_tokens):
            tokens_cut = tokens[:, :-1]
            micro_batch_size = tokens_cut.shape[0]
            _reconfigure_microbatch_calculator(
                rank=app_state.global_rank,
                rampup_batch_size=None,
                global_batch_size=micro_batch_size,
                micro_batch_size=micro_batch_size,
                data_parallel_size=1,
            )
            # For prompt tuned GPT models
            if self.use_soft_prompts:
                if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
                    raise ValueError('compute_logprobs method is not yet supported for pipeline with soft prompts')
                prompt_tags = request["prompt_tags"][idx]
                prompt_tags_to_ids = dict(self.prompt_table)
                prompt_ids = torch.tensor([prompt_tags_to_ids[tag] for tag in prompt_tags])
            else:
                prompt_ids = None

            if self.use_soft_prompts:
                batch_size = len(tokens_cut)
                full_length = len(tokens_cut[0]) + self.num_prompt_tokens
                # Get postion ids for text after soft prompt
                position_ids = torch.arange(
                    start=self.num_prompt_tokens, end=full_length, dtype=torch.long, device=self.device
                )
                position_ids = position_ids.unsqueeze(0).expand_as(tokens_cut).clone()
                # Make attention mask starting with first token in soft prompt
                attention_mask = torch.tril(
                    torch.ones((batch_size, full_length, full_length), device=self.device)
                ).view(batch_size, 1, full_length, full_length)
                attention_mask = attention_mask < 0.5

            else:
                attention_mask, _, position_ids = get_ltor_masks_and_position_ids(
                    data=tokens_cut,
                    eod_token=self.tokenizer.eos_id,
                    reset_position_ids=self.cfg.get('reset_position_ids', False),
                    reset_attention_mask=self.cfg.get('reset_attention_mask', False),
                    eod_mask_loss=self.cfg.get('eod_mask_loss', False),
                )

            # we repeat attention mask to work with apex fwd/bwd function
            attention_mask_repeat = torch.concat([attention_mask for _ in range(micro_batch_size)])
            if self.use_soft_prompts:
                batch = [tokens_cut, attention_mask_repeat, position_ids, prompt_ids]
            else:
                batch = [tokens_cut, attention_mask_repeat, position_ids]
            tensor_shape = [tokens_cut.shape[1], micro_batch_size, self.cfg.hidden_size]
            if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
                output_tensor = forward_backward_pipelining_without_interleaving(
                    forward_step_func=self.get_forward_output_only_func(),
                    batch=batch,
                    model=self.model,
                    forward_only=True,
                    tensor_shape=tensor_shape,
                    dtype=self.autocast_dtype,
                )
            else:
                output_tensor = forward_backward_no_pipelining(
                    forward_step_func=self.get_forward_output_only_func(),
                    batch=batch,
                    model=self.model,
                    forward_only=True,
                    tensor_shape=tensor_shape,
                    dtype=self.autocast_dtype,
                )

            # get output tensor
            if parallel_state.is_pipeline_last_stage():
                output_tensor = output_tensor[0]['logits']
                output_tensor = tensor_parallel.gather_from_tensor_model_parallel_region(output_tensor)

            else:
                output_tensor = torch.zeros(
                    (tokens_cut.shape[0], tokens_cut.shape[1], self.padded_vocab_size), dtype=torch.float
                ).cuda()

            torch.distributed.broadcast(output_tensor, get_last_rank())

            log_probs = []
            for output in output_tensor:
                probs = F.log_softmax(output, dim=1)
                probs = probs[-len(tokens_cut[0]) :]
                log_probs.append(probs)

            for token, prob in zip(tokens, log_probs):
                results.append((self.tokenizer.ids_to_text(token), self.tokenizer.ids_to_tokens(token), prob, [0]))

        # offsets calculation
        for item in results:
            for index, token in enumerate(item[1]):
                if index != len(item[1]) - 1:
                    item[3].append(len(token) + item[3][-1])

        # return prompts in order they were inputted
        response = [0 for i in range(len(positions))]
        for item, index in zip(results, positions):
            response[index] = item

        return response

    def init_new_prompts(self):
        for idx, tag in enumerate(self.cfg.new_prompt_tags):
            init_method = self.cfg.new_prompt_init_methods[idx]

            if init_method == "text":
                init_text = self.cfg.new_prompt_init_text[idx]
                self.init_prompt_from_text(tag, init_text)

            elif init_method == 'random':
                self.init_prompt_from_random(tag)

            else:
                raise AttributeError(
                    f'\n Soft prompt init method {init_method} is not recognized\
                                        please use text or random'
                )

    def init_prompt_from_random(self, prompt_tag):
        prompt_id = self._get_next_prompt_id()
        self.model._init_prompt_from_random(prompt_tag, prompt_id)
        self._add_prompt_tag(prompt_tag, prompt_id)

    def init_prompt_from_text(self, prompt_tag, init_text):
        prompt_id = self._get_next_prompt_id()
        init_token_ids = self.tokenizer.text_to_ids(init_text)
        self.model._init_prompt_from_text(prompt_tag, prompt_id, init_token_ids)
        self._add_prompt_tag(prompt_tag, prompt_id)

    def get_prompt_table(self):
        if hasattr(self, 'prompt_table'):
            return self.prompt_table

    def list_available_models(self):
        return None

    def _get_next_prompt_id(self):
        self.next_prompt_id += 1
        return self.next_prompt_id

    def _add_prompt_tag(self, prompt_tag, prompt_id):
        if not hasattr(self, 'prompt_table'):
            raise AttributeError('Please set "use_soft_prompts" in cfg to True')

        self.prompt_table.add((prompt_tag, prompt_id))
        self.prompts_to_tune.add(prompt_tag)

        # Add new prompt tag to cfg for loading prompt table at inference
        with open_dict(self.cfg):
            self.cfg.existing_prompt_tags = list(self.prompt_table)

    def _vocab_size_with_padding(self, orig_vocab_size, make_vocab_size_divisible_by, tensor_model_parallel_size):
        """Pad vocab size so it is divisible by model parallel size and
        still having GPU friendly size."""

        after = orig_vocab_size
        multiple = make_vocab_size_divisible_by * tensor_model_parallel_size
        while (after % multiple) != 0:
            after += 1
        logging.info(
            f'Padded vocab_size: {after}, original vocab_size: {orig_vocab_size}, dummy tokens: {after - orig_vocab_size}.'
        )
        return after

    def _enable_nvidia_optimizations(self):
        "These optimizations are present in NVIDIA NGC PyTorch Containers"

        # Version check
        nvidia_torch_version = os.getenv('NVIDIA_PYTORCH_VERSION', None)
        if nvidia_torch_version is not None:
            NVIDIA_TORCH_MAJOR = int(nvidia_torch_version.split('.')[0])
            NVIDIA_TORCH_MINOR = int(nvidia_torch_version.split('.')[1])

            # Apex Persistent layer norm is supported from Nvidia PyTorch container v21.11
            if NVIDIA_TORCH_MAJOR < 21 or (NVIDIA_TORCH_MAJOR == 21 and NVIDIA_TORCH_MINOR < 11):
                self.cfg.persist_layer_norm = False

            if NVIDIA_TORCH_MAJOR >= 21 or (NVIDIA_TORCH_MAJOR == 21 and NVIDIA_TORCH_MINOR >= 11):
                # NVFUSER
                torch._C._jit_set_profiling_executor(True)
                torch._C._jit_set_profiling_mode(True)
                torch._C._jit_override_can_fuse_on_cpu(False)
                torch._C._jit_override_can_fuse_on_gpu(False)
                torch._C._jit_set_texpr_fuser_enabled(False)
                torch._C._jit_set_nvfuser_enabled(True)
                torch._C._debug_set_autodiff_subgraph_inlining(False)

        else:
            # Not a Nvidia container. Dependency check is on users
            pass

    def transfer_batch_to_device(self, batch: Any, device: torch.device, dataloader_idx: int) -> Any:
        """ PTL hook: https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#transfer-batch-to-device
            When using pipeline parallelism, we need the global batch to remain on the CPU,
            since the memory overhead will be too high when using a large number of microbatches.
            Microbatches are transferred from CPU to GPU inside the pipeline.
        """
        return batch

    def _validate_trainer(self):
        """ Certain trainer configurations can break training.
            Here we try to catch them and raise an error.
        """
        if self.trainer.accumulate_grad_batches > 1:
            raise ValueError(
                f'Gradient accumulation is done within training_step. trainer.accumulate_grad_batches must equal 1'
            )

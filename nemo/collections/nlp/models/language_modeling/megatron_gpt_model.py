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

import re
from typing import Any, Dict, List, Optional, Union

import torch
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.data.language_modeling.megatron.gpt_dataset import build_train_valid_test_datasets
from nemo.collections.nlp.data.language_modeling.megatron.megatron_batch_samplers import (
    MegatronPretrainingBatchSampler,
    MegatronPretrainingRandomBatchSampler,
)
from nemo.collections.nlp.models.language_modeling.megatron.gpt_model import GPTModel
from nemo.collections.nlp.models.language_modeling.megatron_base_model import MegatronBaseModel
from nemo.collections.nlp.modules.common.megatron.module import Float16Module
from nemo.collections.nlp.modules.common.megatron.utils import (
    average_losses_across_data_parallel_group,
    get_params_for_weight_decay_optimization,
)
from nemo.collections.nlp.modules.common.text_generation_utils import (
    generate,
    get_computeprob_response,
    get_default_length_params,
    get_default_sampling_params,
    megatron_gpt_generate,
)
from nemo.collections.nlp.modules.common.transformer.text_generation import (
    LengthParam,
    OutputType,
    SamplingParam,
    TextGeneration,
)
from nemo.collections.nlp.parts.nlp_overrides import GradScaler
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.core.classes.common import PretrainedModelInfo
from nemo.utils import AppState, logging

try:
    from apex.transformer import parallel_state
    from apex.transformer.pipeline_parallel.schedules.common import build_model
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_pipelining_without_interleaving import (
        forward_backward_pipelining_without_interleaving,
    )
    from apex.transformer.pipeline_parallel.schedules.fwd_bwd_no_pipelining import forward_backward_no_pipelining
    from apex.transformer.pipeline_parallel.utils import get_num_microbatches

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


class MegatronGPTModel(MegatronBaseModel, TextGeneration):
    """
    Megatron GPT pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
        # this prevents base constructor from initializing tokenizer
        self.tokenizer = None
        super().__init__(cfg, trainer=trainer, no_lm_init=True)

        self._validate_trainer()

        # TODO: Not sure how to use lists of modules with PTL.
        # This means we can only use pipeline parallelism without the interleaved schedule.
        self.model = build_model(model_provider_func=self.model_provider_func, wrap_with_ddp=False)[0]

        # We don't need to call it explicitly? Since it is a pytorch lightning hook function
        # self.setup_optimizer_param_groups()

        self.megatron_amp_o2 = cfg.get('megatron_amp_O2', False)
        self.with_distributed_adam = cfg.optim.get('name') == 'distributed_fused_adam'

        if self.with_distributed_adam and not self.megatron_amp_o2:
            raise ValueError(
                "Distributed optimizers require O2. Please set megatron_amp_O2 to True in the model config."
            )

        if self.megatron_amp_o2:

            if not self.with_distributed_adam:
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

        # configuration used for inference
        self._inference_config = None

        # At pipeline-parallel training, set the pipeline stage that the current GPU belongs to skip loading inputs
        # Intemediate stage: doesn't need any inputs
        # Fist pipeline stage: needs only 'tokens' and 'position_ids'
        # Last pipeline stage: needs only 'labels' and 'loss_mask'
        self._is_first_pipe_stage = False
        self._is_intermediate_pipe_stage = False
        self._is_last_pipe_stage = False
        if parallel_state.get_pipeline_model_parallel_world_size() > 1:
            if parallel_state.is_pipeline_first_stage():
                self._is_first_pipe_stage = True
            elif parallel_state.is_pipeline_last_stage():
                self._is_last_pipe_stage = True
            else:
                self._is_intermediate_pipe_stage = True

    def set_inference_config(self, inference_config):
        self._inference_config = inference_config

    def get_inference_config(self):
        return self._inference_config

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
            activations_checkpoint_granularity=self.cfg.get('activations_checkpoint_granularity', None),
            activations_checkpoint_method=self.cfg.get('activations_checkpoint_method', None),
            activations_checkpoint_num_layers=self.cfg.get('activations_checkpoint_num_layers', 1),
            layernorm_epsilon=self.cfg.get('layernorm_epsilon', 1e-5),
            onnx_safe=self.cfg.get('onnx_safe', False),
            persist_layer_norm=self.cfg.get('persist_layer_norm', False),
            sequence_parallel=self.cfg.get('sequence_parallel', False),
            gradient_accumulation_fusion=self.cfg.get('gradient_accumulation_fusion', False),
        )

        return model

    def setup_optimizer_param_groups(self):
        """ModelPT override. Optimizer will get self._optimizer_param_groups"""
        self._optimizer_param_groups = get_params_for_weight_decay_optimization([self.model])

    def setup_optimization(
        self, optim_config: Optional[Union[DictConfig, Dict]] = None, optim_kwargs: Optional[Dict[str, Any]] = None,
    ):
        optim_kwargs = {} if optim_kwargs is None else optim_kwargs.copy()
        if self.with_distributed_adam:
            optim_kwargs['process_group'] = parallel_state.get_data_parallel_group()
            optim_kwargs['param_sync_dtype'] = self.autocast_dtype
            optim_kwargs['contiguous_grad_buffer'] = True
        return super().setup_optimization(optim_config=optim_config, optim_kwargs=optim_kwargs)

    def forward(self, tokens, text_position_ids, attention_mask, labels):
        output_tensor = self.model(tokens, text_position_ids, attention_mask, labels=labels)
        return output_tensor

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

        if self._is_intermediate_pipe_stage:
            # The intermediate pipeline stages do not need any inputs from data loader
            # GPT3 uses decoder with AttnMask:causal, thus doesn't need attention_mask
            batch_for_pipeline = None
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
                sequence_parallel_enabled=self.cfg.get('sequence_parallel', False),
            )
        else:
            # no pipeline parallelism so we reduce grads asynchronously if not using sequence parallelism
            if self.megatron_amp_o2 and not self.cfg.get('sequence_parallel', False):
                if self.with_distributed_adam:
                    custom_sync_context_handler = lambda: self._optimizer.no_sync(greedy_grad_copy=True)
                else:
                    custom_sync_context_handler = self._optimizer.no_sync
            else:
                # TODO: enable async grad all reduce for O1/autocast mixed precision training
                custom_sync_context_handler = None
            losses_reduced_per_micro_batch = forward_backward_no_pipelining(
                forward_step_func=self.get_forward_output_and_loss_func(),
                batch=batch_for_pipeline,
                model=self.model,
                forward_only=False,
                tensor_shape=tensor_shape,
                dtype=self.autocast_dtype,
                grad_scaler=self.trainer.precision_plugin.scaler if self.cfg.precision == 16 else None,
                custom_sync_context_handler=custom_sync_context_handler,
            )

        # only the last stages of the pipeline return losses
        if losses_reduced_per_micro_batch:
            # average loss across micro batches
            loss_tensors_list = [loss_reduced['avg'] for loss_reduced in losses_reduced_per_micro_batch]
            loss_tensor = torch.concat(loss_tensors_list)
            loss_mean = loss_tensor.mean()
        else:
            loss_mean = torch.tensor(0.0).cuda()

        # when using sequence parallelism, the sequence parallel layernorm grads must be all-reduced
        if self.cfg.get('tensor_model_parallel_size', 1) > 1 and self.cfg.get('sequence_parallel', False):
            self.allreduce_sequence_parallel_gradients()

        if self.with_distributed_adam:
            # gradients are reduced internally in distributed optimizer
            pass
        elif self.megatron_amp_o2:
            # when using pipeline parallelism grads must be all-reduced after the pipeline (not asynchronously)
            if self.cfg.get('pipeline_model_parallel_size', 1) > 1 or self.cfg.get('sequence_parallel', False):
                # main grads are stored in the MainParamsOptimizer wrapper
                self._optimizer.allreduce_main_grads()
        else:
            # async grad allreduce is not currently implemented for O1/autocasting mixed precision training
            # so we all-reduce gradients after the pipeline
            self.allreduce_gradients()  # @sangkug we think this is causing memory to blow up (hurts perf)

        if self.cfg.get('pipeline_model_parallel_size', 1) > 1:
            # when using pipeline parallelism the first and last stage must keep embeddings in sync
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

    def allreduce_sequence_parallel_gradients(self):
        """ All-reduce layernorm parameters across model parallel nodes when sequence parallelism is used.
            Modified from megatron-lm:
            https://gitlab-master.nvidia.com/ADLR/megatron-lm/-/blob/3f91f09bb2ab32f9904b47f46f19d2fc3f518ed8/megatron/training.py#L425
        """
        grads = []
        for param in self.model.parameters():
            if getattr(param, 'sequence_parallel_enabled', False):
                if self.megatron_amp_o2:
                    grad = param.main_grad
                else:
                    grad = param.grad
                grads.append(grad.data)
        coalesced = torch._utils._flatten_dense_tensors(grads)
        torch.distributed.all_reduce(coalesced, group=parallel_state.get_tensor_model_parallel_group())
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
            if self.model.share_token_embeddings:
                word_embeddings_weight = self.model.word_embeddings_weight()
                if self.megatron_amp_o2:
                    # O2 recipe stores a "main" copy of weights and grads
                    grad = word_embeddings_weight.main_grad
                else:
                    grad = word_embeddings_weight.grad
                torch.distributed.all_reduce(grad, group=parallel_state.get_embedding_group())

    def get_forward_output_and_loss_func(self):
        def fwd_output_and_loss_func(batch, model):
            if parallel_state.get_pipeline_model_parallel_world_size() == 1:
                batch = [x.cuda(non_blocking=True) for x in batch]
                tokens, labels, loss_mask, attention_mask, position_ids = batch
                attention_mask = attention_mask[0:1]
            else:
                # GPT3 uses only causal mask, which doesn't need attention mask
                if self._is_first_pipe_stage:
                    # Fist pipeline stage needs only the tokens and position_ids
                    tokens = batch[0].cuda(non_blocking=True)
                    position_ids = batch[4].cuda(non_blocking=True)
                    labels, loss_mask, attention_mask = None, None, None
                elif self._is_intermediate_pipe_stage:
                    # Intermediate pipeline stage doesn't need any inputs
                    tokens, labels, loss_mask, attention_mask, position_ids = None, None, None, None, None
                elif self._is_last_pipe_stage:
                    # Last pipeline stage needs only the labels and loss_mask
                    labels = batch[1].cuda(non_blocking=True)
                    loss_mask = batch[2].cuda(non_blocking=True)
                    tokens, attention_mask, position_ids = None, None, None
                else:
                    assert False
            output_tensor = model(tokens, position_ids, attention_mask, labels)

            def loss_func(output_tensor):
                loss = self.loss_func(loss_mask, output_tensor)
                reduced_loss = average_losses_across_data_parallel_group([loss])
                return loss, {'avg': reduced_loss}

            return output_tensor, loss_func

        return fwd_output_and_loss_func

    def get_forward_output_only_func(self):
        def fwd_output_only_func(batch, model):
            extra_arg = {}
            if len(batch) == 3:
                batch = [x.cuda() for x in batch]
                tokens, attention_mask, position_ids = batch
                attention_mask = attention_mask[0:1]
            else:
                (
                    tokens,
                    attention_mask,
                    position_ids,
                    set_inference_key_value_memory,
                    inference_max_sequence_len,
                ) = batch
                tokens = tokens.cuda()
                attention_mask = attention_mask.cuda()
                position_ids = position_ids.cuda()
                attention_mask = attention_mask[0:1]
                extra_arg['set_inference_key_value_memory'] = set_inference_key_value_memory[0].item()
                extra_arg['inference_max_sequence_len'] = inference_max_sequence_len[0].item()
            output_tensor = model(tokens, position_ids, attention_mask, **extra_arg)

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
                sequence_parallel_enabled=self.cfg.get('sequence_parallel', False),
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
        if not outputs:
            return
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
        logging.info('Building GPT datasets.')
        global_batch_size = self.cfg.global_batch_size
        max_train_steps = self.trainer.max_steps
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
                    micro_batch_size=self.cfg.micro_batch_size,
                    global_batch_size=self.cfg.global_batch_size,
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                    drop_last=self.cfg.get('drop_last', True),
                )
            elif self.cfg.data.dataloader_type == 'cyclic':
                batch_sampler = MegatronPretrainingRandomBatchSampler(
                    total_samples=len(dataset),
                    consumed_samples=consumed_samples,
                    micro_batch_size=self.cfg.micro_batch_size,
                    global_batch_size=self.cfg.global_batch_size,
                    data_parallel_rank=parallel_state.get_data_parallel_rank(),
                    data_parallel_size=parallel_state.get_data_parallel_world_size(),
                    drop_last=self.cfg.get('drop_last', True),
                )
            else:
                raise ValueError('cfg.data.dataloader_type must be "single" or "cyclic"')
        else:
            raise ValueError('cfg.data.dataloader_type not found. Must be "single" or "cyclic"')

        return torch.utils.data.DataLoader(
            dataset, batch_sampler=batch_sampler, num_workers=self.cfg.data.num_workers, pin_memory=True,
        )

    def setup(self, stage=None):
        """ PTL hook that is executed after DDP spawns.
            We setup datasets here as megatron datasets require DDP to instantiate.
            See https://pytorch-lightning.readthedocs.io/en/latest/common/lightning_module.html#setup for more information.
        Args:
            stage (str, optional): Can be 'fit', 'validate', 'test' or 'predict'. Defaults to None.
        """
        resume_checkpoint_path = self.trainer._checkpoint_connector.resume_from_checkpoint_fit_path
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
        if hasattr(self, '_train_ds'):
            consumed_samples = self.compute_consumed_samples(0)
            logging.info(
                f'Setting up train dataloader with len(len(self._train_ds)): {len(self._train_ds)} and consumed samples: {consumed_samples}'
            )
            self._train_dl = self.build_pretraining_data_loader(self._train_ds, consumed_samples)

    def setup_validation_data(self, cfg):
        if hasattr(self, '_validation_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up validation dataloader with len(len(self._validation_ds)): {len(self._validation_ds)} and consumed samples: {consumed_samples}'
            )
            self._validation_dl = self.build_pretraining_data_loader(self._validation_ds, consumed_samples)

    def setup_test_data(self, cfg):
        if hasattr(self, '_test_ds'):
            consumed_samples = 0
            logging.info(
                f'Setting up test dataloader with len(len(self._test_ds)): {len(self._test_ds)} and consumed samples: {consumed_samples}'
            )
            self._test_dl = self.build_pretraining_data_loader(self._test_ds, consumed_samples)

    def configure_optimizers(self):
        retval = super().configure_optimizers()

        if self.with_distributed_adam:

            # Initialize params in reverse order
            # Note: Estimate order in which grads are generated in
            # backward pass
            self._optimizer.init_params(reversed(list(self.parameters())))

            # Overlapped communication interferes with grad reductions
            # for pipeline parallelism and sequence parallelism
            if self.cfg.get('pipeline_model_parallel_size', 1) > 1 or self.cfg.get('sequence_parallel', False):
                self._optimizer.overlap_grad_sync = False

        return retval

    def compute_consumed_samples(self, steps_since_resume=0):
        app_state = AppState()
        consumed_samples = (
            self.init_consumed_samples
            + steps_since_resume * app_state.data_parallel_size * self.cfg.micro_batch_size * get_num_microbatches()
        )
        return int(consumed_samples)

    def generate(
        self,
        inputs: Union[List[str], torch.Tensor, List[dict]],
        length_params: LengthParam,
        sampling_params: SamplingParam = None,
    ) -> OutputType:

        # check whether the DDP is initialized
        if parallel_state.is_unitialized():

            def dummy():
                return

            if self.trainer.strategy.launcher is not None:
                self.trainer.strategy.launcher.launch(dummy, trainer=self.trainer)
            self.trainer.strategy.setup_environment()

        # set the default sampling params if it is None.
        # default do greedy sampling
        if sampling_params is None:
            sampling_params = get_default_sampling_params()

        # set the default length params if it is None.
        # default do greedy sampling
        if length_params is None:
            length_params = get_default_length_params()

        return megatron_gpt_generate(self.cuda(), inputs, self.tokenizer, length_params, sampling_params)

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: Optional[int] = None) -> Any:
        inference_config = self.get_inference_config()
        if inference_config is None:
            return None
        else:
            # need to overwrite some configuration, make it immutable
            inference_config = inference_config.copy()
            compute_logprob = inference_config['compute_logprob']
            if compute_logprob:
                del inference_config['compute_logprob']
                inference_config['inputs'] = batch
                inference_config['tokens_to_generate'] = 1
                inference_config['all_probs'] = True
                inference_config["add_BOS"] = False
                inference_config['greedy'] = True
                response = generate(self, **inference_config)
                compute_prob_response = get_computeprob_response(self.tokenizer, response, batch)
                return compute_prob_response
            else:
                del inference_config['compute_logprob']
                inference_config['inputs'] = batch
                return generate(self, **inference_config)

    def list_available_models(self):
        return None

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

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.
        Returns:
            List of available pre-trained models.
        """
        result = []
        result.append(
            PretrainedModelInfo(
                pretrained_model_name="megatron_gpt_345m",
                location="https://api.ngc.nvidia.com/v2/models/nvidia/nemo/megatron_gpt_345m/versions/1/files/megatron_gpt_345m.nemo",
                description="345M parameter GPT generative Megatron model.",
            )
        )
        return result

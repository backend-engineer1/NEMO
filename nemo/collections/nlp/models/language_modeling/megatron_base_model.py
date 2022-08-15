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


import os
from typing import Optional

import torch
from omegaconf import open_dict
from omegaconf.dictconfig import DictConfig
from pytorch_lightning.plugins.precision.native_amp import NativeMixedPrecisionPlugin
from pytorch_lightning.trainer.trainer import Trainer

from nemo.collections.nlp.models.nlp_model import NLPModel
from nemo.collections.nlp.modules.common.megatron.clip_grads import (
    clip_grad_norm_distributed_optimizer,
    clip_grad_norm_fp32,
)
from nemo.collections.nlp.modules.common.megatron.megatron_init import initialize_model_parallel_for_nemo
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer
from nemo.collections.nlp.parts.nlp_overrides import GradScaler
from nemo.core.optim import MainParamsOptimizerWrapper, prepare_lr_scheduler
from nemo.utils import logging

try:
    from apex.transformer import parallel_state

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

__all__ = ["MegatronBaseModel"]


class MegatronBaseModel(NLPModel):
    """
    Megatron base class
    It does the following things:
    1. Initialize the model parallel for nemo given the model parallel parameters.
    2. Turn on all the nvidia optimizations.
    3. If `cfg.tokenizer` is available, it loads the tokenizer and pad the vocab to the correct size for tensor model parallelism.
    4. It help to run `configure_gradient_clipping`, if `grad_clip_pl_default` is set True,  it uses the pytorch lightning default
       gradient clipping. Or if `megatron_amp_o2` is set True, it uses the parameters from optimizer to clip the gradients.
       Otherwise, it uses the parameters calculated in the `setup_optimizer_param_groups` method.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer, no_lm_init=True):
        # FIXME: switch to self._cfg
        if not HAVE_APEX:
            raise ImportError(
                "Apex was not found. Please see the NeMo README for installation instructions: https://github.com/NVIDIA/NeMo#megatron-gpt."
            )
        if trainer is None:
            raise ValueError(f"Trainer cannot be None for Megatron-based models. Please provide a PTL trainer object.")
        # this prevents base constructor from initializing tokenizer
        self.tokenizer = None

        super().__init__(cfg, trainer=trainer, no_lm_init=no_lm_init)

        self._validate_config()

        # used in NVIDIA NGC PyTorch containers
        self._enable_nvidia_optimizations()

        if self._cfg.get('use_cpu_initialization', False) is False:
            torch.cuda.set_device(trainer.local_rank)

        # buffer used during train_step for logging average loss over gradient accumulation steps
        self._reduced_loss_buffer = []

        initialize_model_parallel_for_nemo(
            world_size=trainer.world_size,
            global_rank=trainer.global_rank,
            local_rank=trainer.local_rank,
            tensor_model_parallel_size=cfg.get('tensor_model_parallel_size', 1),
            pipeline_model_parallel_size=cfg.get('pipeline_model_parallel_size', 1),
            pipeline_model_parallel_split_rank=cfg.get('pipeline_model_parallel_split_rank', 0),
            micro_batch_size=cfg.get('micro_batch_size'),
            global_batch_size=cfg.get('global_batch_size'),
            seed=self.cfg.get('seed', 1234),
            apex_transformer_log_level=self.cfg.get('apex_transformer_log_level', 30),
        )

        self.grad_clip_pl_default = False  # use pytorch default for gradient clipping. Default False

        if hasattr(self._cfg, "tokenizer") or (
            hasattr(self._cfg, "encoder_tokenizer") and hasattr(self._cfg, "decoder_tokenizer")
        ):
            # build tokenizer (defaults to nemo supported tokenizers)
            self._build_tokenizer()

            # manipulate vocabulary (e.g., pad vocabulary for better efficiency)
            self._build_vocab()

    def _enable_nvidia_optimizations(self):
        "These optimizations are present in NVIDIA NGC PyTorch Containers"

        # NVIDIA container version check
        nvidia_torch_version = os.getenv('NVIDIA_PYTORCH_VERSION', None)
        if nvidia_torch_version is not None:
            NVIDIA_TORCH_MAJOR = int(nvidia_torch_version.split('.')[0])
            try:
                NVIDIA_TORCH_MINOR = int(nvidia_torch_version.split('.')[1])
            except Exception:
                NVIDIA_TORCH_MINOR = 0

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
            # Not a Nvidia container. NVFUSER Dependency check is on users
            pass

    def _build_tokenizer(self):
        """
        Default tokenizer is based on available nemo tokenizers.
        Override this method to use an external tokenizer.
        All tokenizers are expected to provide compatible interface.
        Override default Encoder-decoder tokenizer to use legacy=True for sentencepiece.
        """
        if hasattr(self._cfg.tokenizer, "sentencepiece_legacy"):
            legacy = self._cfg.tokenizer.sentencepiece_legacy
        else:
            legacy = True if self._cfg.tokenizer.library == 'sentencepiece' else False
        self.tokenizer = get_nmt_tokenizer(
            library=self._cfg.tokenizer.library,
            model_name=self._cfg.tokenizer.type,
            tokenizer_model=self.register_artifact("tokenizer.model", self._cfg.tokenizer.model),
            vocab_file=self.register_artifact("tokenizer.vocab_file", self._cfg.tokenizer.vocab_file),
            merges_file=self.register_artifact("tokenizer.merge_file", self._cfg.tokenizer.merge_file),
            delimiter=self.cfg.tokenizer.get('delimiter', None),
            legacy=legacy,
        )

    def _build_vocab(self):
        """
        Manipulate vocabulary (e.g., pad vocabulary for increased performance)/
        """
        # TODO: add config to allow to disable it?
        self.padded_vocab_size = self._vocab_size_with_padding(
            orig_vocab_size=self.tokenizer.vocab_size,
            make_vocab_size_divisible_by=self._cfg.get('make_vocab_size_divisible_by', 128),
            tensor_model_parallel_size=self._cfg.get('tensor_model_parallel_size', 1),
        )

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

    def _get_parameters(self):
        """
        private method to load all the trainable parameters from optimizer param groups
        """
        params = []
        for param_group in self._optimizer_param_groups:
            for param in param_group['params']:
                params.append(param)
        return params

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

        if self.grad_clip_pl_default:
            # use the default behavior
            return super().configure_gradient_clipping(*args, **kwargs)

        if hasattr(self, 'with_distributed_adam') and self.with_distributed_adam:
            grad_norm = clip_grad_norm_distributed_optimizer(self._optimizer, clip_val)
        else:
            if self.megatron_amp_o2:
                # grep fp32 master parameters for gradient clipping
                parameters = self._optimizer.get_parameters()
            else:
                parameters = self._get_parameters()
            grad_norm = clip_grad_norm_fp32(parameters=parameters, max_norm=clip_val)

        self.log('grad_norm', grad_norm, rank_zero_only=True)

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

                    # Removing the line below because it messes up train_valid_test_num_samples calculation.
                    # self.trainer.fit_loop.max_steps = self.trainer.fit_loop.max_steps + 1

                    # Reset the optimizer update skipped to `None` - this is to prevent scheduler no-ops during
                    # accumulated gradient updates.
                    grad_scaler.optimizer_update_skipped = None

    def configure_optimizers(self):
        self.setup_optimization()

        # Wrap the baseline optimizer with the optimizer class with master parameters
        if (
            self.megatron_amp_o2
            and not (hasattr(self, 'with_distributed_adam') and self.with_distributed_adam)
            and self._optimizer is not None
        ):
            if self.cfg.precision == 'bf16':
                fp32_grad_accum = True
                contiguous_grad_bucket = True
            elif self.cfg.precision == 16:
                fp32_grad_accum = False
                # TODO: contiguous grad bucket for fp16 is also planned to be supported
                contiguous_grad_bucket = False
                raise ValueError(
                    "fp16 training is not yet supported with O2. Please set megatron_amp_O2 to False in the model config."
                )

            # if using tensor parallel only, we automatically use async grad all-reduce
            # if using pipeline parallel or sequence parallel or gradient accumulation fusion, then we disable it
            if self.cfg.get('pipeline_model_parallel_size', 1) == 1 and not (
                self.cfg.get('sequence_parallel', False) or self.cfg.get('gradient_accumulation_fusion', False)
            ):
                async_grad_allreduce = True
            else:
                async_grad_allreduce = False

            if async_grad_allreduce:
                # we need this to be configurable until make_nccl_premul_sum is in public PyTorch.
                # currently cannot be imported in PyTorch 1.12.0
                grad_div_ar_fusion = self.cfg.get('grad_div_ar_fusion', False)
            else:
                grad_div_ar_fusion = False

            self._optimizer = MainParamsOptimizerWrapper(
                self._optimizer,
                fp32_grad_accum=fp32_grad_accum,
                contiguous_grad_bucket=contiguous_grad_bucket,
                async_grad_allreduce=async_grad_allreduce,
                grad_div_ar_fusion=grad_div_ar_fusion,
                grad_allreduce_chunk_size_mb=self.cfg.get('grad_allreduce_chunk_size_mb', 125),
            )

            assert self._trainer.max_steps is not None, "'max_steps' is missing in trainer config."
            if hasattr(self._cfg.optim, 'sched'):
                sched_config = self._cfg.optim.sched
                sched_config['max_steps'] = self._trainer.max_steps
                self._scheduler = prepare_lr_scheduler(
                    optimizer=self._optimizer, scheduler_config=sched_config, train_dataloader=self._train_dl
                )

        if self._scheduler is None:
            return self._optimizer
        else:
            return [self._optimizer], [self._scheduler]

    def _validate_config(self):
        """ Certain configurations might be incompatible or discouraged. We can check for them here."""

        if self.cfg.get('sequence_parallel', False) and self.cfg.get('tensor_model_parallel_size', 1) == 1:
            logging.info(
                "Sequence parallel should only be used with tensor parallel size > 1. Setting sequence parallel to False"
            )
            with open_dict(self.cfg):
                self.cfg.sequence_parallel = False

        if (
            self.cfg.get('gradient_accumulation_fusion', False)
            and self.cfg.get('pipeline_model_parallel_size', 1) == 1
        ):
            logging.info("Gradient accumulation fusion can only be used with pipeline parallel size > 1.")
            with open_dict(self.cfg):
                self.cfg.gradient_accumulation_fusion = False

        if self.cfg.get('gradient_accumulation_fusion', False) and not self.cfg.get('megatron_amp_O2', False):
            logging.info("Gradient accumulation fusion can only be used with megatron amp O2 mixed precision.")
            with open_dict(self.cfg):
                self.cfg.gradient_accumulation_fusion = False

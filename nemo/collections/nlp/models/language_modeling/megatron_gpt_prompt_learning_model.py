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

import re
from typing import Any, Dict, List, Optional, Union

import torch
from omegaconf.dictconfig import DictConfig
from omegaconf.omegaconf import open_dict
from pytorch_lightning.trainer.trainer import Trainer
from torch import Tensor
from torch.utils.data import DataLoader, Dataset

from nemo.collections.nlp.data.language_modeling.megatron.gpt_prompt_learning_dataset import GPTPromptLearningDataset
from nemo.collections.nlp.models.language_modeling.megatron_base_model import MegatronBaseModel
from nemo.collections.nlp.models.language_modeling.megatron_gpt_model import MegatronGPTModel
from nemo.collections.nlp.modules.common import PromptEncoder, PromptTable, VirtualPromptSource, VirtualPromptStyle
from nemo.collections.nlp.modules.common.megatron.utils import average_losses_across_data_parallel_group
from nemo.collections.nlp.modules.common.text_generation_utils import (
    get_default_length_params,
    get_default_sampling_params,
    megatron_gpt_generate,
)
from nemo.collections.nlp.modules.common.transformer.text_generation import LengthParam, SamplingParam, TextGeneration
from nemo.collections.nlp.parts.nlp_overrides import NLPSaveRestoreConnector
from nemo.collections.nlp.parts.utils_funcs import get_last_rank
from nemo.utils import logging

try:
    from apex.transformer import parallel_state

    HAVE_APEX = True

except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False


__all__ = ['MegatronGPTPromptLearningModel']


class MegatronGPTPromptLearningModel(MegatronBaseModel, TextGeneration):
    """
    Model class for prompt-tuning or p-tuning a pretrained Megatron GPT model. 

    Prompt Tuning initalizes virtual prompt embeddings directly from a copy of
    certain token embeddings from the the pretrained GPT model's vocabulary
    and directly tunes these embedding weights. The token embeddings used in 
    initalization are specified by the user in the config file. The model can 
    be prompt-tuned for multiple tasks at once. virtual prompts are stored in a 
    prompt table and can be added or deleted without disrupting virtual prompts 
    for other tasks. 

    P-tuning initializes an LSTM encoder model that generates virtual prompt
    embeddings for every task. Each task shares the same encoder. After ptuning
    is compelete, the learned virtual prompts can be saved to the prompt table
    using add_ptuned_prompts_to_prompt_table(). Thus, if a user wants to add a 
    new virtual prompt via p-tuning, they do not need to retrain on all previous 
    tasks. This gives p-tuning the same task flexiblity as prompt-tuning.
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        super().__init__(cfg, trainer)

        self.cfg = cfg

        # Load pretrained GPT model and tokenizer
        self.model = MegatronGPTModel.restore_from(
            self.register_artifact('language_model_path', cfg.get('language_model_path', None)),
            trainer=trainer,
            save_restore_connector=NLPSaveRestoreConnector(),
        )

        # Freeze all GPT model weights for prompt-tuning/p-tuning
        if not cfg.lm_finetune:
            self.model.freeze()

        self.tokenizer = self.model.tokenizer
        self.float_type = self.model.model.language_model.encoder.layers[0].dtype
        self.hidden_size = self.model.cfg.hidden_size
        self.word_embeddings = self.model.model.language_model.embedding.word_embeddings
        self.existing_tasks = list(self.cfg.get('existing_tasks', []))
        self.new_tasks = list(self.cfg.get('new_tasks', []))

        # Load templates for assigning virtual prompt token positions
        self.load_task_templates(self.cfg.task_templates)

        # Prompt table stores all task embeddings, p-tuning virtual prompts get added to the table after training
        self.prompt_table = PromptTable(
            existing_tasks=self.existing_tasks,
            task_templates=self.task_templates,
            task_id_num_to_name=self.task_id_num_to_name,
            hidden_size=self.hidden_size,
        )

        # Prepare pseudo token ids for virtual/virtual prompt tokens
        self.pseudo_token_base = cfg.pseudo_token_base
        self.pseudo_tokens = [self.pseudo_token_base + str(i) for i in range(self.max_virtual_tokens)]
        self.tokenizer.add_special_tokens({'additional_special_tokens': self.pseudo_tokens})
        self.pseudo_token_ids = self.tokenizer.tokens_to_ids(self.pseudo_tokens)
        self.pseudo_token_ids_start = self.pseudo_token_ids[0]
        self.pad_token_id = self.tokenizer.pad_id if self.tokenizer.pad_id is not None else self.tokenizer.unk_id
        self.virtual_prompt_style = VirtualPromptStyle(cfg.virtual_prompt_style)

        # Prompt tuning stores virtual prompts in the prompt table and tunes their weight directly
        if self.virtual_prompt_style in [VirtualPromptStyle.PROMPT_TUNING, VirtualPromptStyle.INFERENCE]:
            self.virtual_prompt_source = VirtualPromptSource.PROMPT_TABLE

        # P-Tuning uses an LSTM Encoder to produce virtual token embeddings
        elif self.virtual_prompt_style == VirtualPromptStyle.P_TUNING:
            self.virtual_prompt_source = VirtualPromptSource.PROMPT_ENCODER
        else:
            raise ValueError(
                f"\nvirtual prompt style '{cfg.virtual_prompt_style}' not recognized, please use one of 'prompt-tuning' or 'p-tuning'"
            )

        self._reduced_loss_buffer = []
        self._inference_config = None

        if self.trainer.precision == 32:
            self.autocast_dtype = torch.float
        elif self.trainer.precision == 16:
            self.autocast_dtype = torch.half
        elif self.trainer.precision == 'bf16':
            self.autocast_dtype = torch.bfloat16
        else:
            raise ValueError('precision must be in [32, 16, "bf16"]')

    def load_task_templates(self, task_templates):
        """
        Takes in the task template portion of the config and turns  
        it into a table where each task's prompt template and 
        the number of virtual tokens to insert in a given part of 
        the prompt template are specified. 
        """
        self.task_templates = {}
        self.task_id_num_to_name = {}
        self.max_virtual_tokens = 0

        task_id_num = 0
        for task in task_templates:
            self.task_templates[task.taskname] = {
                "prompt_template": task.prompt_template,
                "prompt_template_fields": re.findall("\{(.*?)\}", task.prompt_template),
                "answer_only_loss": task.get("answer_only_loss", False),
                "answer_field": task.get("answer_field", None),
                "truncate_field": task.truncate_field,
                "total_virtual_tokens": task.total_virtual_tokens,
                "virtual_token_splits": task.virtual_token_splits,
                "task_id_num": task_id_num,
            }

            self.max_virtual_tokens = max(self.max_virtual_tokens, task.total_virtual_tokens)
            self.task_id_num_to_name[task_id_num] = task.taskname
            task_id_num += 1

        # Check that all new tasks have the same total num virtual tokens
        # Num virtual tokens for new tasks don't need to match num used for previously tuned tasks
        if self.new_tasks:
            new_task_name = self.new_tasks[0]
            self.total_new_task_virtual_tokens = self.task_templates[new_task_name]["total_virtual_tokens"]

            assert all(
                self.task_templates[taskname]["total_virtual_tokens"] == self.total_new_task_virtual_tokens
                for taskname in self.new_tasks
            ), "Total virtual tokens for each task tuned simultaneously must match. If you want to use a different number of virtual tokens for different tasks, tune them separately."

    def init_new_prompts(self):
        """
        Initialize new virtual prompts to be tuned using prompt tuning 
        """
        for idx, taskname in enumerate(self.new_tasks):
            init_method = self.cfg.prompt_tuning.new_prompt_init_methods[idx].lower()
            total_virtual_tokens = self.task_templates[taskname]["total_virtual_tokens"]

            if init_method == "text":
                init_text = self.cfg.prompt_tuning.new_prompt_init_text[idx]
                init_text_ids = self.tokenizer.text_to_ids(init_text)
                self.prompt_table.init_prompt_from_text(
                    taskname, init_text_ids, self.word_embeddings, total_virtual_tokens
                )

            elif init_method == 'random':
                self.prompt_table.init_prompt_from_random(taskname, total_virtual_tokens)

            else:
                raise AttributeError(
                    f'\nvirtual prompt init method {init_method} is not recognized\
                                        please use one of text or random'
                )

    def init_prompt_encoder(self):
        """
        Init the prompt encoder needed for p-tuning on a new task
        """
        # Total virtual tokens should be the same across all new tasks, so just need one
        new_task = self.new_tasks[0]
        total_virtual_tokens = self.task_templates[new_task]["total_virtual_tokens"]

        self.prompt_encoder = PromptEncoder(
            total_virtual_tokens=total_virtual_tokens,
            hidden_size=self.hidden_size,
            lstm_dropout=self.cfg.p_tuning.dropout,
            num_layers=self.cfg.p_tuning.num_layers,
        )

    def add_ptuned_prompts_to_prompt_table(self):
        """
        Adds all newly p-tuned virtual prompts to the prompt table 
        for inference. p-tuned virtual prompts WILL NOT be further
        tuned once added to the prompt table. After p-tuned prompts
        are added to the prompt table, the prompt encoder weights
        are removed from the model to avoid needing to load unnecessary
        weights during inference or future p-tuning/prompt-tuning.
        """
        # Save p-tuned prompts to prompt table
        for taskname in self.new_tasks:
            device = next(self.word_embeddings.parameters()).device
            tokenized_taskname = torch.tensor(self.tokenizer.text_to_ids(taskname)).to(device)
            taskname_embeddings = self.word_embeddings(tokenized_taskname).unsqueeze(0)
            virtual_prompt_embeddings = self.prompt_encoder(taskname_embeddings=taskname_embeddings).squeeze(0)
            total_virtual_tokens = self.task_templates[taskname]["total_virtual_tokens"]
            self.prompt_table.add_prompt_from_p_tuning_encoder(
                taskname, virtual_prompt_embeddings, total_virtual_tokens
            )

        # Remove prompt encoder from model
        self.prompt_encoder = None

    def freeze_existing_virtual_prompt_params(self):
        """Freeze params of existing virtual prompts that should not be tuned further
        """
        # Only want new prompt tags to be tunable, leave existing prompt tags alone
        for taskname in self.prompt_table.prompt_table.keys():
            if taskname in set(self.new_tasks):
                for params in self.prompt_table.prompt_table[taskname].parameters():
                    params.requires_grad = True
            else:
                for params in self.prompt_table.prompt_table[taskname].parameters():
                    params.requires_grad = False

        # Make sure word embeddings are frozen
        for params in self.word_embeddings.parameters():
            params.requires_grad = False

    def get_model_tasks(self):
        """
        For user to inspect which tasks the model has been
        p-tuned/prompt-tuned to preform.
        """
        tasks = {}
        for taskname in self.prompt_table.prompt_table.keys():
            tasks[taskname] = self.task_templates[taskname].copy()

        return tasks

    def forward(
        self,
        input_ids,
        position_ids,
        attention_mask,
        taskname_ids,
        labels=None,
        inference=True,
        set_inference_key_value_memory=False,
        inference_max_sequence_len=None,
    ):
        """
        Special forward method for p-tuning/prompt-tuning pretrained
        GPT style models. Bypasses the vocab token preprocessing done
        in the MegatronGPT class.
        """
        # Get embeddings for text tokens and insert virtual token embeddings
        if inference:
            input_embeds = self.embed_input_inference(input_ids, taskname_ids)
        else:
            input_embeds = self.embed_input_train(input_ids, taskname_ids)

        position_embeddings = self.model.model.language_model.embedding.position_embeddings(position_ids)
        encoder_input = input_embeds + position_embeddings

        # Call forward on GPT model with preprocessed embeddings
        if self.float_type == torch.float32:
            output = self.model.model(
                input_ids=None,
                position_ids=None,
                encoder_input=encoder_input,
                attention_mask=attention_mask,
                labels=labels,
                set_inference_key_value_memory=set_inference_key_value_memory,
                inference_max_sequence_len=inference_max_sequence_len,
            )
        else:
            with torch.autocast(device_type="cuda", dtype=self.float_type):
                output = self.model.model(
                    input_ids=None,
                    position_ids=None,
                    encoder_input=encoder_input,
                    attention_mask=attention_mask,
                    labels=labels,
                    set_inference_key_value_memory=set_inference_key_value_memory,
                    inference_max_sequence_len=inference_max_sequence_len,
                )

        return output

    def embed_input_train(self, input_ids: Tensor, taskname_ids: Tensor):
        """
        Replaces the virtual tokens in the input_ids with embeddings 
        calculated from either the 'prompt_table' or 'prompt_encoder'. 
        The virtual token placeholders have token_ids listed in
        `self.pseudo_token_ids`.

        params:
            input_ids: the input token ids
            taskname_ids: the NLP task tag token ids
        returns:
            the token embedding for the LM model.
        """
        # Replace virtual token ids with padding for forward pass through vocab embeddings
        discrete_token_ids = input_ids.clone()
        discrete_token_ids[(input_ids >= self.pseudo_token_ids_start)] = self.pad_token_id
        discrete_token_embeds = self.word_embeddings(discrete_token_ids).clone()

        # Find the indicies where virtual tokens should be inserted
        virtual_token_locations = input_ids >= self.pseudo_token_ids_start

        # If there are no virtual tokens, just return discrete token embeds
        if not virtual_token_locations.any():
            return discrete_token_embeds

        # Get virtual token embeddings from the prompt table or prompt encoder
        if self.virtual_prompt_source == VirtualPromptSource.PROMPT_TABLE:
            virtual_token_embeds = [self.prompt_table(task_id_num) for task_id_num in taskname_ids]
            virtual_token_embeds = torch.stack(virtual_token_embeds)

        elif self.virtual_prompt_source == VirtualPromptSource.PROMPT_ENCODER:
            taskname_embeddings = self.word_embeddings(taskname_ids)
            virtual_token_embeds = self.prompt_encoder(taskname_embeddings=taskname_embeddings)

        # Create index template specifying where virtual token embeddings should be placed
        batch_size, _, embedding_size = discrete_token_embeds.shape
        virtual_token_index = virtual_token_locations.nonzero().reshape((batch_size, -1, 2))[:, :, 1][:, :, None]
        virtual_token_index = virtual_token_index.expand(
            batch_size, self.total_new_task_virtual_tokens, embedding_size
        )

        # Make sure discrete_token_embeds and virtual_token_embeds share the same dtype
        discrete_token_embeds = discrete_token_embeds.type(virtual_token_embeds.dtype)

        # Insert virtual token embeddings where they belong amoung the discrete token embeddings
        discrete_token_embeds.scatter_(1, virtual_token_index, virtual_token_embeds)
        input_embeds = discrete_token_embeds

        return input_embeds

    def embed_input_inference(self, input_ids: Tensor, taskname_ids: Tensor):
        """
        Replaces the virtual tokens in the input_ids with embeddings the 
        'prompt_table' only. The virtual token placeholders each have their
        own unique token_id within `self.pseudo_token_ids` to facilitate 
        placing the virtual tokens in their correct locations at each 
        decoding time step. 

        params:
            input_ids: the input token ids
            taskname_ids: the NLP task tag token ids
        returns:
            the token embedding for the LM model.
        """
        batch_size, seq_length = input_ids.shape

        # Replace virtual token ids with padding for forward pass through vocab embeddings
        discrete_token_ids = input_ids.clone()
        discrete_token_ids[(input_ids >= self.pseudo_token_ids_start)] = self.pad_token_id
        discrete_token_embeds = self.word_embeddings(discrete_token_ids).clone()

        # Find the indicies where virtual tokens should be inserted
        virtual_token_locations = input_ids >= self.pseudo_token_ids_start
        virtual_token_locations = virtual_token_locations.unsqueeze(-1)
        virtual_token_locations = virtual_token_locations.expand(batch_size, seq_length, self.hidden_size)

        # If there are no virtual tokens, just return discrete token embeds
        if not virtual_token_locations.any():
            return discrete_token_embeds

        # Convert virtual token vocab_id to virtual token embedding idx
        virtual_token_ids = input_ids.clone()
        virtual_token_ids = torch.sub(virtual_token_ids, self.pseudo_token_ids_start)
        virtual_token_ids = torch.clamp(virtual_token_ids, min=0)

        # Only get needed virtual token embeddings from the prompt table according to virtual token ids
        virtual_token_embeds = [self.prompt_table(taskname_ids[i], virtual_token_ids[i]) for i in range(batch_size)]
        virtual_token_embeds = torch.stack(virtual_token_embeds)

        # Make sure discrete_token_embeds and virtual_token_embeds share the same dtype
        discrete_token_embeds = discrete_token_embeds.type(virtual_token_embeds.dtype)

        # Put virtual and discrete token embs in their correct locations for final output
        input_embeds = torch.where(virtual_token_locations, virtual_token_embeds, discrete_token_embeds)
        return input_embeds

    def training_step(self, batch, batch_idx):
        input_ids, labels, loss_mask, position_ids, attention_mask, taskname_ids = batch
        output = self.forward(input_ids, position_ids, attention_mask, taskname_ids, labels, inference=False)
        output_tensor, encoder_hidden_states = output
        loss = self.model.loss_func(loss_mask, output_tensor)
        self.log('train_loss', loss)

        # Reduced loss for logging.
        reduced_loss = average_losses_across_data_parallel_group([loss])

        # Cache reduced loss while accumulating gradients
        self._reduced_loss_buffer.append(reduced_loss[0])

        if (batch_idx + 1) % self.trainer.accumulate_grad_batches == 0:
            # Reduced loss for logging.
            average_reduced_loss = sum(self._reduced_loss_buffer) / len(self._reduced_loss_buffer)
            self.log('reduced_train_loss', average_reduced_loss, prog_bar=True)
            lr = self._optimizer.param_groups[0]['lr']
            self.log('lr', lr)
            self.log('global_step', self.trainer.global_step, prog_bar=True)
            self._reduced_loss_buffer = []

        return loss

    def validation_step(self, batch, batch_idx):
        input_ids, labels, loss_mask, position_ids, attention_mask, taskname_ids = batch

        with torch.no_grad():
            output = self.forward(input_ids, position_ids, attention_mask, taskname_ids, labels, inference=False)
            output_tensor, encoder_hidden_states = output
            loss = self.model.loss_func(loss_mask, output_tensor)
            self.log('val_loss', loss)

            return loss

    def validation_epoch_end(self, outputs):
        averaged_loss = average_losses_across_data_parallel_group(outputs)

        # we can only log on one rank if it is rank zero so we broadcast from last rank
        torch.distributed.broadcast(averaged_loss, get_last_rank())
        self.log('val_loss', averaged_loss, prog_bar=True, rank_zero_only=True)

    def test_step(self, batch, batch_idx):
        return self.validation_step(batch, batch_idx)

    def test_epoch_end(self, outputs):
        averaged_loss = average_losses_across_data_parallel_group(outputs)
        logging.info(f'test_loss: {averaged_loss[0]}')

    def on_train_end(self):
        # Save p-tuned prompts to prompt table for inference or future task training
        if self.virtual_prompt_style == VirtualPromptStyle.P_TUNING:
            self.add_ptuned_prompts_to_prompt_table()
            logging.info(f"All p-tuned prompts where moved to the prompt table.")

        self.virtual_prompt_style = VirtualPromptStyle.INFERENCE
        self.virtual_prompt_source = VirtualPromptSource.PROMPT_TABLE

        # Move new tags to existing tag list for loading during inference later
        with open_dict(self.cfg):
            self.cfg.existing_tasks = self.existing_tasks + self.new_tasks
            self.cfg.new_tasks = []
            self.cfg.virtual_prompt_style = VirtualPromptStyle.INFERENCE.value

        # Save the best nemo model
        self.save_to(save_path=self.cfg.nemo_path)
        logging.info(f"The final model was saved to {self.cfg.nemo_path}")

    def setup(self, stage=None):
        if stage == 'predict' or self.virtual_prompt_style == VirtualPromptStyle.INFERENCE:
            self.freeze_existing_virtual_prompt_params()
            return

        self.setup_test_data()
        if stage == 'test':
            return

        if self.virtual_prompt_style == VirtualPromptStyle.PROMPT_TUNING:
            self.init_new_prompts()
        elif self.virtual_prompt_style == VirtualPromptStyle.P_TUNING:
            self.init_prompt_encoder()

        self.setup_training_data()
        self.setup_validation_data()
        self.freeze_existing_virtual_prompt_params()

    def setup_training_data(self, training_data_config=None):
        if self.cfg.data.get('train_ds', None):
            self._train_ds, self._train_dl = self.build_virtual_prompt_dataset(
                dataset_paths=self.cfg.data.train_ds,
                batch_size=self.cfg.batch_size,
                for_train=True,
                drop_last=True,
                shuffle=True,
                num_workers=self.cfg.data.num_workers,
                pin_memory=True,
            )

    def setup_validation_data(self, validation_data_config=None):
        if self.cfg.data.get('validation_ds', None):
            self._validation_ds, self._validation_dl = self.build_virtual_prompt_dataset(
                dataset_paths=self.cfg.data.validation_ds,
                batch_size=self.cfg.batch_size,
                for_train=True,
                drop_last=True,
                shuffle=False,
                num_workers=self.cfg.data.num_workers,
                pin_memory=True,
            )

    def setup_test_data(self, test_data_config=None):
        if self.cfg.data.get('test_ds', None):
            self._test_ds, self._test_dl = self.build_virtual_prompt_dataset(
                dataset_paths=self.cfg.data.test_ds,
                batch_size=self.cfg.batch_size,
                for_train=False,
                drop_last=False,
                shuffle=False,
                num_workers=self.cfg.data.num_workers,
                pin_memory=True,
            )

    def build_virtual_prompt_dataset(
        self, dataset_paths, batch_size, for_train, drop_last, shuffle, num_workers, pin_memory
    ):
        dataset = GPTPromptLearningDataset(
            datasets=dataset_paths,
            tokenizer=self.tokenizer,
            virtual_prompt_source=self.virtual_prompt_source,
            task_templates=self.task_templates,
            pseudo_tokens=self.pseudo_tokens,
            pad_token_id=self.pad_token_id,
            max_seq_length=self.cfg.data.get('max_seq_length', self.model.cfg.max_position_embeddings),
            min_seq_length=self.cfg.data.get('min_seq_length', 1),
            add_bos=self.cfg.data.get('add_bos', False),
            add_eos=self.cfg.data.get('add_eos', True),
            for_train=for_train,
        )

        dataloader = torch.utils.data.DataLoader(
            dataset,
            collate_fn=dataset.collate_fn,
            batch_size=batch_size,
            drop_last=drop_last,
            shuffle=shuffle,
            num_workers=num_workers,
            pin_memory=pin_memory,
        )

        return dataset, dataloader

    def generate(
        self,
        inputs: Union[List[str], torch.Tensor, List[dict]],
        length_params: LengthParam,
        sampling_params: SamplingParam = None,
    ):

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
            sampling_params["add_BOS"] = self.cfg.data.get("add_bos", False)

        if length_params is None:
            length_params = get_default_length_params()

        # Preprocess inputs to be what they need to be for the generate code
        dataset = GPTPromptLearningDataset(
            datasets=inputs,
            tokenizer=self.tokenizer,
            virtual_prompt_source=self.virtual_prompt_source,
            task_templates=self.task_templates,
            pseudo_tokens=self.pseudo_tokens,
            pad_token_id=self.pad_token_id,
            max_seq_length=self.cfg.data.get('max_seq_length', self.model.cfg.max_position_embeddings),
            min_seq_length=self.cfg.data.get('min_seq_length', 1),
            add_bos=sampling_params["add_BOS"],
            add_eos=False,
            for_train=False,
        )
        task_ids, processed_inputs = dataset.get_all_examples(tokens_to_generate=length_params['max_length'])
        self.model.model.parallel_output = False

        # Call same generate code as in MegatronGPT
        return megatron_gpt_generate(
            self.cuda(), processed_inputs, self.tokenizer, length_params, sampling_params, task_ids
        )

    def predict_step(self, batch: Any, batch_idx: int, dataloader_idx: Optional[int] = None) -> Any:
        inference_config = self.get_inference_config()
        if inference_config is None:
            return None
        else:
            length_params: LengthParam = {
                "max_length": inference_config["tokens_to_generate"],
                "min_length": inference_config["min_tokens_to_generate"],
            }

            sampling_params: SamplingParam = {
                "use_greedy": inference_config["greedy"],
                "temperature": inference_config["temperature"],
                "top_k": inference_config["top_k"],
                "top_p": inference_config["top_p"],
                "repetition_penalty": inference_config["repetition_penalty"],
                "add_BOS": inference_config["add_BOS"],
                "all_probs": inference_config["all_probs"],
                "compute_logprob": inference_config["compute_logprob"],
            }
            return self.generate(batch, length_params, sampling_params)

    def set_inference_config(self, inference_config):
        self._inference_config = inference_config

    def get_inference_config(self):
        return self._inference_config

    def set_input_tensor(self, input_tensor):
        """Set input tensor to be used instead of forward()'s input.
        When doing pipeline parallelism the input from the previous
        stage comes from communication, not from the input, so the
        model's forward_step_func won't have it. This function is thus
        used by internal code to bypass the input provided by the
        forward_step_func"""
        self.input_tensor = input_tensor

    def get_forward_output_only_func(self):
        """
        Used for generate method only for now.
        """

        def fwd_output_only_func(batch, model):
            extra_arg = {}
            (
                tokens,
                attention_mask,
                position_ids,
                task_ids,
                set_inference_key_value_memory,
                inference_max_sequence_len,
            ) = batch

            tokens = tokens.cuda()
            attention_mask = attention_mask.cuda()
            position_ids = position_ids.cuda()
            task_ids = task_ids.cuda()
            extra_arg['set_inference_key_value_memory'] = set_inference_key_value_memory[0].item()
            extra_arg['inference_max_sequence_len'] = inference_max_sequence_len[0].item()

            output_tensor = model(tokens, position_ids, attention_mask, task_ids, **extra_arg)

            def id_func(output_tensor):
                return output_tensor, {'logits': output_tensor}

            return output_tensor, id_func

        return fwd_output_only_func

    @classmethod
    def list_available_models(cls):
        pass

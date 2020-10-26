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

import math
import random

import omegaconf
import pytest
import pytorch_lightning as pl
import torch
import torch.optim

from nemo.core import config, optim
from nemo.core.optim.lr_scheduler import AVAILABLE_SCHEDULERS
from nemo.core.optim.optimizers import AVAILABLE_OPTIMIZERS


class TempModel(torch.nn.Module):
    def __init__(self):
        super(TempModel, self).__init__()
        self.layer = torch.nn.Linear(5, 1)

    def forward(self, x):
        x = self.layer(x)
        return x


class OptCounter(torch.optim.SGD):
    def __init__(self, *args, **kwargs):
        self.count = 0
        super().__init__(*args, **kwargs)

    def step(self, closure=None):
        self.count += 1
        super().step(closure)


class RandomDataset(torch.utils.data.Dataset):
    def __init__(self, dataset_len):
        super().__init__()
        self.__dataset_len = dataset_len

    def __getitem__(self, *args):
        return torch.randn(2)

    def __len__(self):
        return self.__dataset_len


class ExampleModel(pl.LightningModule):
    def __init__(self, batch_size, dataset_len, drop_last, max_steps):
        super().__init__()
        self.l1 = torch.nn.modules.Linear(in_features=2, out_features=1)
        self.__batch_size = batch_size
        self.__dataset_len = dataset_len
        self.__drop_last = drop_last
        self.max_steps = max_steps

    def train_dataloader(self):
        dataset = RandomDataset(self.__dataset_len)
        return torch.utils.data.DataLoader(dataset, batch_size=self.__batch_size, drop_last=self.__drop_last)

    def training_step(self, batch, batch_idx):
        output = self.l1(batch)
        output = torch.nn.functional.l1_loss(output, torch.ones(output.size()).to(output.device))
        return {"loss": output}

    def configure_optimizers(self):
        self.my_opt = OptCounter(self.parameters(), lr=0.02)
        return self.my_opt


class Callback(pl.callbacks.Callback):
    def on_train_end(self, trainer, module):
        assert (
            trainer.global_step == module.my_opt.count
        ), f"{trainer.global_step} != {module.my_opt.count} != {module.max_steps}"
        assert (
            trainer.global_step == module.max_steps
        ), f"{trainer.global_step} != {module.my_opt.count} != {module.max_steps}"


class TestOptimizersSchedulers:
    INITIAL_LR = 0.1
    MIN_LR = 1e-3
    MAX_STEPS = 10

    @pytest.mark.unit
    def test_get_optimizer(self):
        model = TempModel()

        for opt_name in AVAILABLE_OPTIMIZERS.keys():
            opt_cls = optim.get_optimizer(opt_name)
            opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

            assert isinstance(opt, AVAILABLE_OPTIMIZERS[opt_name])

    @pytest.mark.unit
    def test_register_optimizer(self):
        class TempOpt(torch.optim.SGD):
            pass

        class TempOptParams(config.optimizers.SGDParams):
            pass

        optim.register_optimizer('TempOpt', TempOpt, TempOptParams)

        model = TempModel()
        opt_cls = optim.get_optimizer('TempOpt')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        assert isinstance(opt, TempOpt)

    @pytest.mark.unit
    def test_optim_config_parse_bypass(self):
        basic_optim_config = {'weight_decay': 0.001, 'betas': [0.8, 0.5]}
        parsed_params = optim.parse_optimizer_args('novograd', basic_optim_config)
        assert parsed_params['weight_decay'] == basic_optim_config['weight_decay']
        assert parsed_params['betas'][0] == basic_optim_config['betas'][0]
        assert parsed_params['betas'][1] == basic_optim_config['betas'][1]

        dict_config = omegaconf.OmegaConf.create(basic_optim_config)
        parsed_params = optim.parse_optimizer_args('novograd', dict_config)
        assert parsed_params['weight_decay'] == dict_config['weight_decay']
        assert parsed_params['betas'][0] == dict_config['betas'][0]
        assert parsed_params['betas'][1] == dict_config['betas'][1]

    @pytest.mark.unit
    def test_optim_config_parse_arg_by_name(self):
        basic_optim_config = {'name': 'auto', 'weight_decay': 0.001, 'betas': [0.8, 0.5]}
        parsed_params = optim.parse_optimizer_args('novograd', basic_optim_config)
        assert parsed_params['weight_decay'] == basic_optim_config['weight_decay']
        assert parsed_params['betas'][0] == basic_optim_config['betas'][0]
        assert parsed_params['betas'][1] == basic_optim_config['betas'][1]

        dict_config = omegaconf.OmegaConf.create(basic_optim_config)
        parsed_params = optim.parse_optimizer_args('novograd', dict_config)
        assert parsed_params['weight_decay'] == dict_config['weight_decay']
        assert parsed_params['betas'][0] == dict_config['betas'][0]
        assert parsed_params['betas'][1] == dict_config['betas'][1]

        with pytest.raises(omegaconf.errors.ConfigKeyError):
            optim.parse_optimizer_args('sgd', dict_config)

    @pytest.mark.unit
    def test_optim_config_parse_arg_by_target(self):
        basic_optim_config = {
            'target': 'nemo.core.config.NovogradParams',
            'params': {'weight_decay': 0.001, 'betas': [0.8, 0.5]},
        }
        basic_optim_config = omegaconf.OmegaConf.create(basic_optim_config)
        parsed_params = optim.parse_optimizer_args('novograd', basic_optim_config)
        assert parsed_params['weight_decay'] == basic_optim_config['params']['weight_decay']
        assert parsed_params['betas'][0] == basic_optim_config['params']['betas'][0]
        assert parsed_params['betas'][1] == basic_optim_config['params']['betas'][1]

        dict_config = omegaconf.OmegaConf.create(basic_optim_config)
        parsed_params = optim.parse_optimizer_args('novograd', dict_config)
        assert parsed_params['weight_decay'] == dict_config['params']['weight_decay']
        assert parsed_params['betas'][0] == dict_config['params']['betas'][0]
        assert parsed_params['betas'][1] == dict_config['params']['betas'][1]

        # Names are ignored when passing class path
        # This will be captured during optimizer instantiation
        output_config = optim.parse_optimizer_args('sgd', dict_config)
        sgd_config = vars(config.SGDParams())
        novograd_config = vars(config.NovogradParams())

        assert set(output_config.keys()) != set(sgd_config.keys())
        assert set(output_config.keys()) == set(novograd_config)

    @pytest.mark.unit
    def test_get_scheduler(self):
        model = TempModel()
        optimizer = optim.Novograd(model.parameters(), lr=self.INITIAL_LR)

        for sched_name in AVAILABLE_SCHEDULERS.keys():
            sched_cls = optim.lr_scheduler.get_scheduler(sched_name)

            try:
                sched = sched_cls(optimizer)
                assert isinstance(sched, AVAILABLE_SCHEDULERS[sched_name])
                continue
            except Exception:
                pass

            try:
                sched = sched_cls(optimizer, max_steps=self.MAX_STEPS)
                assert isinstance(sched, AVAILABLE_SCHEDULERS[sched_name])
                continue
            except Exception:
                pass

    @pytest.mark.unit
    def test_register_scheduler(self):
        class TempSched(optim.lr_scheduler.CosineAnnealing):
            pass

        class TempSchedParams(config.schedulers.CosineAnnealingParams):
            pass

        optim.lr_scheduler.register_scheduler('TempSched', TempSched, TempSchedParams)

        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)
        sched_cls = optim.lr_scheduler.get_scheduler('TempSched')
        sched = sched_cls(opt, max_steps=self.MAX_STEPS)

        assert isinstance(sched, TempSched)

    @pytest.mark.unit
    def test_sched_config_parse_simple(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        basic_sched_config = {'name': 'CosineAnnealing', 'max_steps': 10}
        scheduler_setup = optim.lr_scheduler.prepare_lr_scheduler(opt, basic_sched_config)
        assert isinstance(scheduler_setup['scheduler'], optim.lr_scheduler.CosineAnnealing)

        dict_config = omegaconf.OmegaConf.create(basic_sched_config)
        scheduler_setup = optim.lr_scheduler.prepare_lr_scheduler(opt, dict_config)
        assert isinstance(scheduler_setup['scheduler'], optim.lr_scheduler.CosineAnnealing)

    @pytest.mark.unit
    def test_sched_config_parse_from_cls(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        basic_sched_config = {
            'target': 'nemo.core.config.CosineAnnealingParams',
            'params': {'min_lr': 0.1},
            'max_steps': self.MAX_STEPS,
        }
        scheduler_setup = optim.lr_scheduler.prepare_lr_scheduler(opt, basic_sched_config)
        assert isinstance(scheduler_setup['scheduler'], optim.lr_scheduler.CosineAnnealing)

        dict_config = omegaconf.OmegaConf.create(basic_sched_config)
        scheduler_setup = optim.lr_scheduler.prepare_lr_scheduler(opt, dict_config)
        assert isinstance(scheduler_setup['scheduler'], optim.lr_scheduler.CosineAnnealing)

    @pytest.mark.unit
    def test_WarmupPolicy(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.WarmupPolicy(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] == self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.WarmupPolicy(opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 4:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] == self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_WarmupHoldPolicy(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.WarmupHoldPolicy(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] == self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.WarmupHoldPolicy(opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 4:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] == self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup + Hold steps available
        policy = optim.lr_scheduler.WarmupHoldPolicy(
            opt, warmup_steps=5, hold_steps=3, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 4:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] == self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_WarmupAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.WarmupAnnealing(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.WarmupAnnealing(opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup + Hold steps available
        policy = optim.lr_scheduler.WarmupHoldPolicy(
            opt, warmup_steps=5, hold_steps=3, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 4:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] == self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_SquareAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.SquareAnnealing(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.SquareAnnealing(opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_SquareRootAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.SquareRootAnnealing(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.SquareRootAnnealing(
            opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_CosineAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.CosineAnnealing(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.CosineAnnealing(opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_PolynomialDecayAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.PolynomialDecayAnnealing(
            opt, power=2, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.PolynomialDecayAnnealing(
            opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_PolynomialHoldDecayAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.PolynomialHoldDecayAnnealing(
            opt, power=2, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.PolynomialHoldDecayAnnealing(
            opt, power=2, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup + Hold steps available
        policy = optim.lr_scheduler.PolynomialHoldDecayAnnealing(
            opt, warmup_steps=5, hold_steps=3, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR, power=2
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 4:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            elif i <= 8:
                assert policy.get_last_lr()[0] == self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    def test_InverseSquareRootAnnealing(self):
        model = TempModel()
        opt_cls = optim.get_optimizer('novograd')
        opt = opt_cls(model.parameters(), lr=self.INITIAL_LR)

        # No warmup case
        policy = optim.lr_scheduler.InverseSquareRootAnnealing(opt, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR)
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr == self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            assert policy.get_last_lr()[0] <= self.INITIAL_LR
            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

        # Warmup steps available
        policy = optim.lr_scheduler.InverseSquareRootAnnealing(
            opt, warmup_steps=5, max_steps=self.MAX_STEPS, min_lr=self.MIN_LR
        )
        initial_lr = policy.get_last_lr()[0]

        assert initial_lr < self.INITIAL_LR

        for i in range(self.MAX_STEPS):
            if i <= 5:
                assert policy.get_last_lr()[0] <= self.INITIAL_LR
            else:
                assert policy.get_last_lr()[0] < self.INITIAL_LR

            opt.step()
            policy.step()

        policy.step()
        final_lr = policy.get_last_lr()[0]

        assert final_lr == self.MIN_LR

    @pytest.mark.unit
    @pytest.mark.run_only_on('CPU')
    def test_max_step_computation(self, cleanup_local_folder):
        def train(max_epochs, accumulate_grad_batches, num_processes, batch_size, dataset_len, drop_last):
            trainer = pl.Trainer(
                max_epochs=max_epochs,
                accelerator="ddp_cpu",
                num_processes=num_processes,
                accumulate_grad_batches=accumulate_grad_batches,
                checkpoint_callback=False,
                progress_bar_refresh_rate=0,
                weights_summary=None,
            )
            max_steps = optim.lr_scheduler.compute_max_steps(
                max_epochs, accumulate_grad_batches, num_processes, dataset_len, batch_size, drop_last
            )
            model = ExampleModel(batch_size, dataset_len, drop_last, max_steps)
            trainer.callbacks.append(Callback())
            trainer.fit(model)

        # This test will break once we and lightning upgrade to pytorch 1.7.0 due to a bug fix in pytorch 1.7.0
        train(31, accumulate_grad_batches=1, num_processes=9, batch_size=60, dataset_len=1613, drop_last=True)

        for _ in range(5):
            drop_last = bool(random.randint(0, 1))
            accumulate_grad_batches = random.randint(1, 10)
            max_epochs = random.randint(4, 20)
            num_processes = random.randint(1, 5)
            dataset_len = random.randint(20, num_processes * 500)
            batch_size = random.randint(math.ceil(5.0 / num_processes), min(dataset_len // num_processes, 128))
            train(max_epochs, accumulate_grad_batches, num_processes, batch_size, dataset_len, drop_last)

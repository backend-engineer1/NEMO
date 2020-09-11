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

from nemo.collections.tts.models.glow_tts import GlowTTSModel
from nemo.collections.tts.models.squeezewave import SqueezeWaveModel
from nemo.collections.tts.models.tacotron2 import Tacotron2Model
from nemo.collections.tts.models.waveglow import WaveGlowModel

__all__ = ["GlowTTSModel", "SqueezeWaveModel", "Tacotron2Model", "WaveGlowModel"]

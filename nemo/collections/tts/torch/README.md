# Torch TTS Collection

This section of code can be used by installing the requirements inside our *requirements.txt* and *requirements_torch_tts.txt*.

## Install

This collection can be installed in the following ways:
 - pip install from github
    > ```bash
    > pip install git+https://github.com/nvidia/NeMo.git#egg=nemo_toolkit[torch_tts]
    > ```
  - inside a requirements file
    > `git+https://github.com/nvidia/NeMo.git#egg=nemo_toolkit[torch_tts]`
  - cloning from github, and then installing
    > ```bash
    >  git clone https://github.com/nvidia/NeMo.git && cd NeMo && pip install ".[torch_tts]"
    > ```

## Usage

We can check that lightning is not installed by checking pip:
```bash
pip list | grep lightning
```
Now even though lightning isn't installed, we can still use parts from the `torch_tts` collection.

### TTS Dataset: example

Let's import our dataset class, loop through the batches and do simple task: calculate pitch statistics. Note that in the sample .json files, we only have text
and audio. Our dataset will then create supplementary data (e.g. pitch) and store them in `supplementary_folder`. You can find config in `tts_dataset.yaml`.

```python
import torch
from hydra.utils import instantiate
from omegaconf import OmegaConf
from tqdm import tqdm

cfg = OmegaConf.load("nemo/collections/tts/torch/tts_dataset.yaml")

dataset = instantiate(cfg.tts_dataset)
dataloader = torch.utils.data.DataLoader(dataset, 1, collate_fn=dataset._collate_fn, num_workers=1)

pitch_list = []
for batch in tqdm(dataloader, total=len(dataloader)):
    audios, audio_lengths, tokens, tokens_lengths, pitches, pitches_lengths = batch
    pitch = pitches.squeeze(0)
    pitch_list.append(pitch[pitch != 0])

pitch_tensor = torch.cat(pitch_list)
print(f"PITCH_MEAN, PITCH_STD = {pitch_tensor.mean().item()}, {pitch_tensor.std().item()}")
```

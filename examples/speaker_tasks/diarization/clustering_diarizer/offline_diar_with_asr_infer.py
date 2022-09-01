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


from omegaconf import OmegaConf

from nemo.collections.asr.parts.utils.decoder_timestamps_utils import ASR_TIMESTAMPS
from nemo.collections.asr.parts.utils.diarization_utils import ASR_DIAR_OFFLINE
from nemo.core.config import hydra_runner
from nemo.utils import logging


"""
This script demonstrates how to run offline speaker diarization with asr.
Usage:
python offline_diar_with_asr_infer.py \
    diarizer.manifest_filepath=<path to manifest file> \
    diarizer.out_dir='demo_asr_output' \
    diarizer.speaker_embeddings.model_path=<pretrained modelname or path to .nemo> \
    diarizer.asr.model_path=<pretrained modelname or path to .nemo> \
    diarizer.asr.parameters.asr_based_vad=True \
    diarizer.speaker_embeddings.parameters.save_embeddings=False

Check out whole parameters in ./conf/offline_diarization_with_asr.yaml and their meanings.
For details, have a look at <NeMo_git_root>/tutorials/speaker_tasks/Speaker_Diarization_Inference.ipynb
Currently, the following NGC models are supported:

    stt_en_quartznet15x5
    stt_en_citrinet*
    stt_en_conformer_ctc*

"""


@hydra_runner(config_path="../conf/inference", config_name="diar_infer_telephonic.yaml")
def main(cfg):

    logging.info(f'Hydra config: {OmegaConf.to_yaml(cfg)}')

    # ASR inference for words and word timestamps
    asr_ts_decoder = ASR_TIMESTAMPS(**cfg.diarizer)
    asr_model = asr_ts_decoder.set_asr_model()
    word_hyp, word_ts_hyp = asr_ts_decoder.run_ASR(asr_model)

    # Create a class instance for matching ASR and diarization results
    asr_diar_offline = ASR_DIAR_OFFLINE(**cfg.diarizer)
    asr_diar_offline.word_ts_anchor_offset = asr_ts_decoder.word_ts_anchor_offset

    # Diarization inference for speaker labels
    diar_hyp, diar_score = asr_diar_offline.run_diarization(cfg, word_ts_hyp)
    total_riva_dict = asr_diar_offline.get_transcript_with_speaker_labels(diar_hyp, word_hyp, word_ts_hyp)

    if diar_score is not None:

        metric, mapping_dict = diar_score
        DER_result_dict = asr_diar_offline.gather_eval_results(metric, mapping_dict, total_riva_dict)
        WDER_dict = asr_diar_offline.get_WDER(total_riva_dict, DER_result_dict)
        asr_diar_offline.print_errors(DER_result_dict, WDER_dict)


if __name__ == '__main__':
    main()

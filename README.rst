
|status| |documentation| |license| |lgtm_grade| |lgtm_alerts| |black|

.. |status| image:: http://www.repostatus.org/badges/latest/active.svg
  :target: http://www.repostatus.org/#active
  :alt: Project Status: Active – The project has reached a stable, usable state and is being actively developed.

.. |documentation| image:: https://readthedocs.com/projects/nvidia-nemo/badge/?version=main
  :alt: Documentation
  :target: https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/

.. |license| image:: https://img.shields.io/badge/License-Apache%202.0-brightgreen.svg
  :target: https://github.com/NVIDIA/NeMo/blob/master/LICENSE
  :alt: NeMo core license and license for collections in this repo

.. |lgtm_grade| image:: https://img.shields.io/lgtm/grade/python/g/NVIDIA/NeMo.svg?logo=lgtm&logoWidth=18
  :target: https://lgtm.com/projects/g/NVIDIA/NeMo/context:python
  :alt: Language grade: Python

.. |lgtm_alerts| image:: https://img.shields.io/lgtm/alerts/g/NVIDIA/NeMo.svg?logo=lgtm&logoWidth=18
  :target: https://lgtm.com/projects/g/NVIDIA/NeMo/alerts/
  :alt: Total alerts

.. |black| image:: https://img.shields.io/badge/code%20style-black-000000.svg
  :target: https://github.com/psf/black
  :alt: Code style: black

.. _main-readme:

**NVIDIA NeMo**
===============

Introduction
------------

NVIDIA NeMo is a conversational AI toolkit built for researchers working on automatic speech recognition (ASR), natural language processing (NLP), and text-to-speech synthesis (TTS).
The primary objective of NeMo is to help researchers from industry and academia to reuse prior work (code and pretrained models) and make it easier to create new `conversational AI models <https://developer.nvidia.com/conversational-ai#started>`_.

`Pre-trained NeMo models. <https://catalog.ngc.nvidia.com/models?query=nemo&orderBy=weightPopularDESC>`_ 

`Introductory video. <https://www.youtube.com/embed/wBgpMf_KQVw>`_

Key Features
------------

* Speech processing
    * `Automatic Speech Recognition (ASR) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/asr/intro.html>`_
        * Supported models: Jasper, QuartzNet, CitriNet, Conformer-CTC, Conformer-Transducer, ContextNet, ...
        * Supports CTC and Transducer/RNNT losses/decoders
        * Beam Search decoding
        * `Language Modelling for ASR <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/asr/asr_language_modeling.html>`_: N-gram LM in fusion with Beam Search decoding, Neural Rescoring with Transformer
        * Streaming and Buffered ASR (CTC/Transdcer) - `Chunked Inference Examples <https://github.com/NVIDIA/NeMo/tree/main/examples/asr/asr_chunked_inference>`_
    * `Speech Classification and Speech Command Recognition <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/asr/speech_classification/intro.html>`_: MatchboxNet (Command Recognition)
    * `Voice activity Detection (VAD) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/stable/asr/speech_classification/models.html#marblenet-vad>`_: MarbleNet
    * `Speaker Recognition <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/asr/speaker_recognition/intro.html>`_: TitaNet, ECAPA_TDNN, SpeakerNet
    * `Speaker Diarization <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/asr/speaker_diarization/intro.html>`_: TitaNet, ECAPA_TDNN, SpeakerNet
    * `Pretrained models on different languages. <https://ngc.nvidia.com/catalog/collections/nvidia:nemo_asr>`_: English, Spanish, German, Russian, Chinese, French, Italian, Polish, ...
    * `NGC collection of pre-trained speech processing models. <https://ngc.nvidia.com/catalog/collections/nvidia:nemo_asr>`_
* Natural Language Processing
    * `Compatible with Hugging Face Transformers and NVIDIA Megatron <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/megatron_finetuning.html>`_
    * `Neural Machine Translation (NMT) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/machine_translation.html>`_
    * `Punctuation and Capitalization <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/punctuation_and_capitalization.html>`_
    * `Token classification (named entity recognition) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/token_classification.html>`_
    * `Text classification <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/text_classification.html>`_
    * `Joint Intent and Slot Classification <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/joint_intent_slot.html>`_
    * `BERT pre-training <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/bert_pretraining.html>`_
    * `Question answering <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/question_answering.html>`_
    * `GLUE benchmark <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/glue_benchmark.html>`_
    * `Information retrieval <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/information_retrieval.html>`_
    * `Entity Linking <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/entity_linking.html>`_
    * `Dialogue State Tracking <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/sgd_qa.html>`_
    * `Neural Duplex Text Normalization <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/text_normalization.html>`_
    * `Prompt Tuning <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/nlp/megatron_finetuning.html#prompt-tuning>`_
    * `NGC collection of pre-trained NLP models. <https://ngc.nvidia.com/catalog/collections/nvidia:nemo_nlp>`_
* `Speech synthesis (TTS) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/tts/intro.html#>`_
    * Spectrogram generation: Tacotron2, GlowTTS, TalkNet, FastPitch, FastSpeech2, Mixer-TTS, Mixer-TTS-X
    * Vocoders: WaveGlow, SqueezeWave, UniGlow, MelGAN, HiFiGAN, UnivNet
    * End-to-end speech generation: FastPitch_HifiGan_E2E, FastSpeech2_HifiGan_E2E
    * `NGC collection of pre-trained TTS models. <https://ngc.nvidia.com/catalog/collections/nvidia:nemo_tts>`_
* `Tools <https://github.com/NVIDIA/NeMo/tree/main/tools>`_
    * `Text Processing (text normalization and inverse text normalization) <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/tools/text_processing_deployment.html>`_
    * `CTC-Segmentation tool <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/tools/ctc_segmentation.html>`_
    * `Speech Data Explorer <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/tools/speech_data_explorer.html>`_: a dash-based tool for interactive exploration of ASR/TTS datasets


Built for speed, NeMo can utilize NVIDIA's Tensor Cores and scale out training to multiple GPUs and multiple nodes.

Requirements
------------

1) Python 3.6, 3.7 or 3.8
2) Pytorch 1.10.0 or above
3) NVIDIA GPU for training

Documentation
-------------

.. |main| image:: https://readthedocs.com/projects/nvidia-nemo/badge/?version=main
  :alt: Documentation Status
  :scale: 100%
  :target: https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/

.. |stable| image:: https://readthedocs.com/projects/nvidia-nemo/badge/?version=stable
  :alt: Documentation Status
  :scale: 100%
  :target:  https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/stable/

+---------+-------------+------------------------------------------------------------------------------------------------------------------------------------------+
| Version | Status      | Description                                                                                                                              |
+=========+=============+==========================================================================================================================================+
| Latest  | |main|      | `Documentation of the latest (i.e. main) branch. <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/main/>`_                  |
+---------+-------------+------------------------------------------------------------------------------------------------------------------------------------------+
| Stable  | |stable|    | `Documentation of the stable (i.e. most recent release) branch. <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/stable/>`_ |
+---------+-------------+------------------------------------------------------------------------------------------------------------------------------------------+

Tutorials
---------
A great way to start with NeMo is by checking `one of our tutorials <https://docs.nvidia.com/deeplearning/nemo/user-guide/docs/en/stable/starthere/tutorials.html>`_.

Getting help with NeMo
----------------------
FAQ can be found on NeMo's `Discussions board <https://github.com/NVIDIA/NeMo/discussions>`_. You are welcome to ask questions or start discussions there.


Installation
------------

Pip
~~~
Use this installation mode if you want the latest released version.

.. code-block:: bash

    apt-get update && apt-get install -y libsndfile1 ffmpeg
    pip install Cython
    pip install nemo_toolkit['all']

.. note::

    Depending on the shell used, you may need to use ``"nemo_toolkit[all]"`` instead in the above command.

Pip from source
~~~~~~~~~~~~~~~
Use this installation mode if you want the a version from particular GitHub branch (e.g main).

.. code-block:: bash

    apt-get update && apt-get install -y libsndfile1 ffmpeg
    pip install Cython
    python -m pip install git+https://github.com/NVIDIA/NeMo.git@{BRANCH}#egg=nemo_toolkit[all]


From source
~~~~~~~~~~~
Use this installation mode if you are contributing to NeMo.

.. code-block:: bash

    apt-get update && apt-get install -y libsndfile1 ffmpeg
    git clone https://github.com/NVIDIA/NeMo
    cd NeMo
    ./reinstall.sh

.. note::

    If you only want the toolkit without additional conda-based dependencies, you may replace ``reinstall.sh``
    with ``pip install -e .`` when your PWD is the root of the NeMo repository.

RNNT
~~~~
Note that RNNT requires numba to be installed from conda.

.. code-block:: bash

  conda remove numba
  pip uninstall numba
  conda install -c conda-forge numba

Megatron GPT
~~~~~~~~~~~~
Megatron GPT training requires NVIDIA Apex to be installed.

.. code-block:: bash

    git clone https://github.com/NVIDIA/apex
    cd apex
    git checkout c8bcc98176ad8c3a0717082600c70c907891f9cb
    pip install -v --disable-pip-version-check --no-cache-dir --global-option="--cpp_ext" --global-option="--cuda_ext" --global-option="--fast_layer_norm" ./

Docker containers:
~~~~~~~~~~~~~~~~~~
To build a nemo container with Dockerfile from a branch, please run 

.. code-block:: bash

    DOCKER_BUILDKIT=1 docker build -f Dockerfile -t nemo:latest .


If you chose to work with main branch, we recommend using NVIDIA's PyTorch container version 22.01-py3 and then installing from GitHub.

.. code-block:: bash

    docker run --gpus all -it --rm -v <nemo_github_folder>:/NeMo --shm-size=8g \
    -p 8888:8888 -p 6006:6006 --ulimit memlock=-1 --ulimit \
    stack=67108864 --device=/dev/snd nvcr.io/nvidia/pytorch:22.01-py3

Examples
--------

Many examples can be found under `"Examples" <https://github.com/NVIDIA/NeMo/tree/stable/examples>`_ folder.


Contributing
------------

We welcome community contributions! Please refer to the  `CONTRIBUTING.md <https://github.com/NVIDIA/NeMo/blob/stable/CONTRIBUTING.md>`_ CONTRIBUTING.md for the process.

Publications
------------

We provide an ever growing list of publications that utilize the NeMo framework. Please refer to `PUBLICATIONS.md <https://github.com/NVIDIA/NeMo/blob/main/PUBLICATIONS.md>`_. We welcome the addition of your own articles to this list !

Citation
--------

.. code-block:: bash

  @article{kuchaiev2019nemo,
    title={Nemo: a toolkit for building ai applications using neural modules},
    author={Kuchaiev, Oleksii and Li, Jason and Nguyen, Huyen and Hrinchuk, Oleksii and Leary, Ryan and Ginsburg, Boris and Kriman, Samuel and Beliaev, Stanislav and Lavrukhin, Vitaly and Cook, Jack and others},
    journal={arXiv preprint arXiv:1909.09577},
    year={2019}
  }

License
-------
NeMo is under `Apache 2.0 license <https://github.com/NVIDIA/NeMo/blob/stable/LICENSE>`_.

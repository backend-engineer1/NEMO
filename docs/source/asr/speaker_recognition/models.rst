Models
======

Examples of config files for the following quartznet model can be found in the ``<NeMo_git_root>/examples/speaker_recogniton/conf`` directory.

For more information about the config files and how they should be structured, see the :doc:`./configs` page.

Pretrained checkpoints for all of these models, as well as instructions on how to load them, can be found on the :doc:`./results` page.
You can use the available checkpoints for immediate inference, or fine-tune them on your own datasets.
The Checkpoints page also contains benchmark results for the available speaker recognition models.

.. _SpeakerNet_model:

SpeakerNet
-----------

The model is based on the QuartzNet ASR architecture :cite:`sr-models-koluguri2020speakernet`
comprising of an encoder and decoder structure. We use the encoder of the QuartzNet model as a top-level feature extractor, and feed the output to the statistics pooling layer, where
we compute the mean and variance across channel dimensions to capture the time-independent utterance-level speaker features.

The QuartzNet encoder used for speaker embeddings shown in figure below has the following structure: a QuartzNet BxR
model has B blocks, each with R sub-blocks. Each sub-block applies the following operations: a 1D convolution, batch norm, ReLU, and dropout. All sub-blocks in a block have the same number of output channels. These blocks are connected with residual connections. We use QuartzNet with 3 blocks, 2 sub-blocks, and 512 channels, as the Encoder for Speaker Embeddings. All conv layers have stride 1 and dilation 1.


    .. image:: images/ICASPP_SpeakerNet.png
        :align: center
        :alt: speakernet model
        :scale: 40%

Top level acoustic Features, obtained from the output of
encoder are used to compute intermediate features that are
then passed to the decoder for getting utterance level speaker
embeddings. The intermediate time-independent features are
computed using a statistics pooling layer, where we compute the mean and standard deviation of features across
time-channels, to get a time-independent feature representation S of size Batch_size × 3000.
The intermediate features, S are passed through the Decoder consisting of two layers each of output size 512 for a
linear transformation from S to the final number of classes
N for the larger (L) model, and a single linear layer of output size 256 to the final number of classes N for the medium
(M) model. We extract q-vectors after the final linear layer
of fixed size 512, 256 for SpeakerNet-L and SpeakerNet-M
models respectively.

SpeakerNet models can be instantiated using the :class:`EncDecSpeakerLabelModel<nemo.collections.asr.models.EncDecSpeakerLabelModel>` class.


References
-----------

.. bibliography:: ../asr_all.bib
    :style: plain
    :labelprefix: SR-MODELS
    :keyprefix: sr-models-

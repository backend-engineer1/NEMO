Models
======

This section gives a brief overview of the models that NeMo's ASR collection currently supports.

Each of these models can be used with the example ASR scripts (in the ``<NeMo_git_root>/examples/asr`` directory) by
specifying the model architecture in the config file used. Examples of config files for each model can be found in 
the ``<NeMo_git_root>/examples/asr/conf`` directory.

For more information about the config files and how they should be structured, refer to the :doc:`./configs` section.

Pretrained checkpoints for all of these models, as well as instructions on how to load them, can be found in the :doc:`./results` 
section. You can use the available checkpoints for immediate inference, or fine-tune them on your own datasets. The checkpoints section 
also contains benchmark results for the available ASR models.

.. _Jasper_model:

Jasper
------

Jasper ("Just Another Speech Recognizer") :cite:`asr-models-li2019jasper` is a deep time delay neural network (TDNN) comprising of 
blocks of 1D-convolutional layers. The Jasper family of models are denoted as ``Jasper_[BxR]`` where ``B`` is the number of blocks 
and ``R`` is the number of convolutional sub-blocks within a block. Each sub-block contains a 1-D convolution, batch normalization, 
ReLU, and dropout:

    .. image:: images/jasper_vertical.png
        :align: center
        :alt: japer model
        :scale: 50%

Jasper models can be instantiated using the :class:`EncDecCTCModel<nemo.collections.asr.models.EncDecCTCModel>` class.

QuartzNet
---------

QuartzNet :cite:`asr-models-kriman2019quartznet` is a version of Jasper :cite:`asr-models-li2019jasper` model with separable
convolutions and larger filters. It can achieve performance similar to Jasper but with an order of magnitude fewer parameters. 
Similarly to Jasper, the QuartzNet family of models are denoted as ``QuartzNet_[BxR]`` where ``B`` is the number of blocks and ``R`` 
is the number of convolutional sub-blocks within a block. Each sub-block contains a 1-D *separable* convolution, batch normalization, 
ReLU, and dropout:

    .. image:: images/quartz_vertical.png
        :align: center
        :alt: quartznet model
        :scale: 40%

QuartzNet models can be instantiated using the :class:`EncDecCTCModel<nemo.collections.asr.models.EncDecCTCModel>` class.

Citrinet
--------

Citrinet is a version of QuartzNet :cite:`asr-models-kriman2019quartznet` that extends ContextNet :cite:`asr-models-han2020contextnet`,
utilizing subword encoding (via Word Piece tokenization) and Squeeze-and-Excitation mechanism :cite:`asr-models-hu2018squeeze` to
obtain highly accurate audio transcripts while utilizing a non-autoregressive CTC based decoding scheme for efficient inference.

    .. image:: images/citrinet_vertical.png
        :align: center
        :alt: citrinet model
        :scale: 50%

Citrinet models can be instantiated using the :class:`EncDecCTCModelBPE<nemo.collections.asr.models.EncDecCTCModelBPE>` class.

.. _Conformer-CTC_model:

Conformer-CTC
-------------

Conformer-CTC is a CTC-based variant of the Conformer model introduced in :cite:`asr-models-gulati2020conformer`. Conformer-CTC has a
similar encoder as the original Conformer but uses CTC loss and decoding instead of RNNT loss, which makes it a non-autoregressive model.
We also drop the LSTM decoder and instead use a linear decoder on the top of the encoder. This model uses the combination of 
self-attention and convolution modules to achieve the best of the two approaches, the self-attention layers can learn the global 
interaction while the convolutions efficiently capture the local correlations. The self-attention modules support both regular 
self-attention with absolute positional encoding, and also Transformer-XL's self-attention with relative positional encodings.

Here is the overall architecture of the encoder of Conformer-CTC:

    .. image:: images/conformer_ctc.png
        :align: center
        :alt: Conformer-CTC Model
        :scale: 50%

This model supports both the sub-word level and character level encodings. You can find more details on the config files for the 
Conformer-CTC models at :doc:`Conformer-CTC <./configs.html#conformer-ctc>`. The variant with sub-word encoding is a BPE-based model 
which can be instantiated using the :class:`EncDecCTCModelBPE<nemo.collections.asr.models.EncDecCTCModelBPE>` class, while the
character-based variant is based on :class:`EncDecCTCModel<nemo.collections.asr.models.EncDecCTCModel>`.

References
----------

.. bibliography:: asr_all.bib
    :style: plain
    :labelprefix: ASR-MODELS
    :keyprefix: asr-models-

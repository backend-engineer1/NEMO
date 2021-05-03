Checkpoints
===========

There are two main ways to load pretrained checkpoints in NeMo:

* Using the :code:`restore_from()` method to load a local checkpoint file (``.nemo``), or
* Using the :code:`from_pretrained()` method to download and set up a checkpoint from NGC.

Refer to the following sections for instructions and examples for each.

Note that these instructions are for loading fully trained checkpoints for evaluation or fine-tuning. For resuming an unfinished 
training experiment, use the Experiment Manager to do so by setting the ``resume_if_exists`` flag to ``True``.

Loading Local Checkpoints
-------------------------

NeMo automatically saves checkpoints of a model that is trained in a ``.nemo`` format. Alternatively, to manually save the model at any 
point, issue :code:`model.save_to(<checkpoint_path>.nemo)`.

If there is a local ``.nemo`` checkpoint that you'd like to load, use the :code:`restore_from()` method:

.. code-block:: python

  import nemo.collections.asr as nemo_asr
  model = nemo_asr.models.<MODEL_BASE_CLASS>.restore_from(restore_path="<path/to/checkpoint/file.nemo>")

Where the model base class is the ASR model class of the original checkpoint, or the general ``ASRModel`` class.

NGC Pretrained Checkpoints
--------------------------

The ASR collection has checkpoints of several models trained on various datasets for a variety of tasks. These checkpoints are 
obtainable via NGC `NeMo Automatic Speech Recognition collection <https://ngc.nvidia.com/catalog/models/nvidia:nemospeechmodels>`_.
The model cards on NGC contain more information about each of the checkpoints available.

The tables below list the ASR models available from NGC. The models can be accessed via the :code:`from_pretrained()` method inside
the ASR Model class. In general, you can load any of these models with code in the following format:

.. code-block:: python

  import nemo.collections.asr as nemo_asr
  model = nemo_asr.models.ASRModel.from_pretrained(model_name="<MODEL_NAME>")

Where the model name is the value under "Model Name" entry in the tables below.

For example, to load the base English QuartzNet model for speech recognition, run:

.. code-block:: python

  model = nemo_asr.models.ASRModel.from_pretrained(model_name="QuartzNet15x5Base-En")

You can also call :code:`from_pretrained()` from the specific model class (such as :code:`EncDecCTCModel`
for QuartzNet) if you need to access a specific model functionality.

If you would like to programatically list the models available for a particular base class, you can use the
:code:`list_available_models()` method.

.. code-block:: python

  nemo_asr.models.<MODEL_BASE_CLASS>.list_available_models()

Transcribing/Inference
^^^^^^^^^^^^^^^^^^^^^^

To perform inference and transcribe a sample of speech after loading the model, use the ``transcribe()`` method:

.. code-block:: python

    model.transcribe(paths2audio_files=[list of audio files], batch_size=BATCH_SIZE, logprobs=False)

Setting the argument ``logprobs`` to ``True`` returns the log probabilities instead of transcriptions. For more information, see :doc:`./api.html#modules`.
The audio files should be 16KHz monochannel wav files.

Automatic Speech Recognition Models
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. csv-table::
   :file: data/asr_results.csv
   :align: left
   :widths: 30, 30, 40
   :header-rows: 1


   
Speech Recognition (Languages)
------------------------------

English
^^^^^^^
.. csv-table::
   :file: data/benchmark_en.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

Mandarin
^^^^^^^^
.. csv-table::
   :file: data/benchmark_zh.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

German
^^^^^^
.. csv-table::
   :file: data/benchmark_de.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

Polish
^^^^^^
.. csv-table::
   :file: data/benchmark_pl.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

Italian
^^^^^^^
.. csv-table::
   :file: data/benchmark_it.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

Russian
^^^^^^^
.. csv-table::
   :file: data/benchmark_ru.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1

-----------------------------

Spanish
^^^^^^^
.. csv-table::
   :file: data/benchmark_es.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1


-----------------------------

Catalan
^^^^^^^
.. csv-table::
   :file: data/benchmark_ca.csv
   :align: left
   :widths: 40, 10, 50
   :header-rows: 1



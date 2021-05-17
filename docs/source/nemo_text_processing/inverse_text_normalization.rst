Inverse Text Normalization
==========================

Inverse text normalization (ITN) is a part of the Automatic Speech Recognition (ASR) post-processing pipeline.
ITN is the task of converting the raw spoken output of the ASR model into its written form to improve text readability.

For example, 
`"in nineteen seventy"` -> `"in 1975"` 
and `"it costs one hundred and twenty three dollars"` -> `"it costs $123"`.

NeMo ITN :cite:`textprocessing-itn-zhang2021nemo` is based on WFST-grammars :cite:`textprocessing-itn-Mohri2009`. We also provide a deployment route to C++ using `Sparrowhawk <https://github.com/google/sparrowhawk>`_ :cite:`textprocessing-itn-sparrowhawk` -- an open-source version of Google Kestrel :cite:`textprocessing-itn-ebden2015kestrel`.
See :doc:`Text Procesing Deployment <../tools/text_processing_deployment>` for details.

.. note::

    For more details, see the tutorial `NeMo/tutorials/text_processing/Inverse_Text_Normalization.ipynb <https://github.com/NVIDIA/NeMo/blob/main/tutorials/text_processing/Inverse_Text_Normalization.ipynb>`__ in `Google's Colab <https://colab.research.google.com/github/NVIDIA/NeMo/blob/main/tutorials/text_processing/Inverse_Text_Normalization.ipynb>`_.






Classes
----------------------------------


The base class for every grammar is :class:`GraphFst<nemo_text_processing.text_normalization.GraphFst>`.
This tool is designed as a two-stage application: 1. `classification` of the input into semiotic tokens and 2. `verbalization` into written form.
For every stage and every semiotic token class there is a corresponding grammar, e.g. :class:`taggers.CardinalFst<nemo_text_processing.inverse_text_normalization.taggers.cardinal.CardinalFst>`
and :class:`verbalizers.CardinalFst<nemo_text_processing.inverse_text_normalization.verbalizers.cardinal.CardinalFst>`.
Together, they compose the final grammars :class:`ClassifyFst<nemo_text_processing.inverse_text_normalization.ClassifyFst>` and 
:class:`VerbalizeFinalFst<nemo_text_processing.inverse_text_normalization.VerbalizeFinalFst>` that are compiled into WFST and used for inference.






.. autoclass:: nemo_text_processing.inverse_text_normalization.ClassifyFst
    :show-inheritance:
    :members:

.. autoclass:: nemo_text_processing.inverse_text_normalization.VerbalizeFinalFst
    :show-inheritance:
    :members:
 

Prediction
----------------------------------

Example prediction run:

.. code::

    python run_prediction.py  --input=<INPUT_TEXT_FILE> --output=<OUTPUT_PATH>  [--verbose]


The input is expected to be lower-cased. Punctuation are outputted with separating spaces after semiotic tokens, e.g. `"i see, it is ten o'clock..."` -> `"I see, it is 10:00  .  .  ."`.
Inner-sentence white-space characters in the input are not maintained. 



Data Cleaning for Evaluation
----------------------------------

.. code::

    python clean_eval_data.py  --input=<INPUT_TEXT_FILE>



Evaluation
----------------------------------

Example evaluation run on (cleaned) `Google's text normalization dataset <https://www.kaggle.com/richardwilliamsproat/text-normalization-for-english-russian-and-polish>`__ :cite:`textprocessing-itn-sproat2016rnn`:

.. code::

    python run_evaluation.py  --input=./en_with_types/output-00001-of-00100 [--cat CLASS_CATEGORY] [--filter]




References
----------

.. bibliography:: textprocessing_all.bib
    :style: plain
    :labelprefix: TEXTPROCESSING-ITN
    :keyprefix: textprocessing-itn-
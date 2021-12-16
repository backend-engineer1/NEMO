# Copyright (c) 2021, NVIDIA CORPORATION.  All rights reserved.
# Copyright 2015 and onwards Google, Inc.
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

import sys
from unicodedata import category

from nemo_text_processing.text_normalization.en.graph_utils import GraphFst

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = False
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class PunctuationFst(GraphFst):
    """
    Finite state transducer for classifying punctuation
        e.g. a, -> tokens { name: "a" } tokens { name: "," }

    Args:
        deterministic: if True will provide a single transduction option,
            for False multiple transduction are generated (used for audio-based normalization)

    """

    def __init__(self, deterministic: bool = True):
        super().__init__(name="punctuation", kind="classify", deterministic=deterministic)

        s = "!#%&\'()*+,-./:;<=>?@^_`{|}~\""

        punct_unicode = [chr(i) for i in range(sys.maxunicode) if category(chr(i)).startswith("P")]
        punct_unicode.remove('[')
        punct_unicode.remove(']')
        punct = pynini.union(*s) | pynini.union(*punct_unicode)

        self.graph = punct
        self.fst = (pynutil.insert("name: \"") + self.graph + pynutil.insert("\"")).optimize()

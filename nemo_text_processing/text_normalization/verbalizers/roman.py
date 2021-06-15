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

from nemo_text_processing.text_normalization.graph_utils import NEMO_NOT_QUOTE, GraphFst
from nemo_text_processing.text_normalization.verbalizers.ordinal import OrdinalFst

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class RomanFst(GraphFst):
    """
    Finite state transducer for verbalizing roman numerals
        e.g. tokens { roman { integer: "one" } } -> one

    Args:
        deterministic: if True will provide a single transduction option,
            for False multiple transduction are generated (used for audio-based normalization)
    """

    def __init__(self, deterministic: bool = True):
        super().__init__(name="roman", kind="verbalize", deterministic=deterministic)
        suffix = OrdinalFst().suffix

        integer = pynini.closure(NEMO_NOT_QUOTE)
        integer |= pynini.closure(pynutil.insert("the "), 0, 1) + integer @ suffix
        graph = pynutil.delete("integer: \"") + integer + pynutil.delete("\"")
        delete_tokens = self.delete_tokens(graph)
        self.fst = delete_tokens.optimize()

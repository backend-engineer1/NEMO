# Copyright (c) 2021, NVIDIA CORPORATION & AFFILIATES.  All rights reserved.
# Copyright 2017 Google Inc.
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

# Adapted from https://github.com/google/TextNormalizationCoveringGrammars
# Russian minimally supervised number grammar.

from nemo_text_processing.text_normalization.en.graph_utils import GraphFst, insert_space

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class CardinalFst(GraphFst):
    """
    Finite state transducer for classifying cardinals, e.g. 
        "1 001" ->  cardinal { integer: "тысяча один" }

    Args:
        number_names: number_names for cardinal and ordinal numbers
        alternative_formats: alternative number formats
        deterministic: if True will provide a single transduction option,
            for False multiple transduction are generated (used for audio-based normalization)
    """

    def __init__(self, number_names: dict, alternative_formats: dict, deterministic: bool = False):
        super().__init__(name="cardinal", kind="classify", deterministic=deterministic)

        cardinal_default = number_names['cardinal_number_names']

        one_thousand_alternative = alternative_formats['one_thousand_alternative']
        separators = alternative_formats['separators']

        cardinal_numbers = cardinal_default | pynini.compose(cardinal_default, one_thousand_alternative)
        cardinal_numbers = pynini.compose(separators, cardinal_numbers)
        self.optional_graph_negative = pynini.closure(
            pynutil.insert("negative: ") + pynini.cross("-", "\"true\"") + insert_space, 0, 1
        )
        self.cardinal_numbers = cardinal_numbers
        self.cardinal_numbers_with_optional_negative = (
            self.optional_graph_negative + pynutil.insert("integer: \"") + cardinal_numbers + pynutil.insert("\"")
        )

        # "03" -> remove leading zeros and verbalize
        leading_zeros = pynini.closure(pynini.cross("0", ""))
        self.cardinal_numbers_with_leading_zeros = (leading_zeros + cardinal_numbers).optimize()
        final_graph = (
            self.optional_graph_negative
            + pynutil.insert("integer: \"")
            + self.cardinal_numbers_with_leading_zeros
            + pynutil.insert("\"")
        ).optimize()

        final_graph = self.add_tokens(final_graph)
        self.fst = final_graph.optimize()

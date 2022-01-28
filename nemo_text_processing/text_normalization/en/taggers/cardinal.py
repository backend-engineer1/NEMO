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


from nemo_text_processing.text_normalization.en.graph_utils import (
    NEMO_ALPHA,
    NEMO_DIGIT,
    NEMO_NOT_SPACE,
    NEMO_SIGMA,
    GraphFst,
    insert_space,
)
from nemo_text_processing.text_normalization.en.taggers.date import get_hundreds_graph
from nemo_text_processing.text_normalization.en.utils import get_abs_path

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class CardinalFst(GraphFst):
    """
    Finite state transducer for classifying cardinals, e.g. 
        -23 -> cardinal { negative: "true"  integer: "twenty three" } }

    Args:
        deterministic: if True will provide a single transduction option,
            for False multiple transduction are generated (used for audio-based normalization)
    """

    def __init__(self, deterministic: bool = True):
        super().__init__(name="cardinal", kind="classify", deterministic=deterministic)
        # TODO repalce to have "oh" as a default for "0"
        graph = pynini.Far(get_abs_path("data/numbers/cardinal_number_name.far")).get_fst()
        self.graph_hundred_component_at_least_one_none_zero_digit = (
            pynini.closure(NEMO_DIGIT, 2, 3) | pynini.difference(NEMO_DIGIT, pynini.accep("0"))
        ) @ graph
        self.graph = (
            pynini.closure(NEMO_DIGIT, 1, 3)
            + pynini.closure(pynini.closure(pynutil.delete(","), 0, 1) + NEMO_DIGIT + NEMO_DIGIT + NEMO_DIGIT)
        ) @ graph

        graph_digit = pynini.string_file(get_abs_path("data/numbers/digit.tsv"))
        graph_zero = pynini.string_file(get_abs_path("data/numbers/zero.tsv"))

        single_digits_graph = pynini.invert(graph_digit | graph_zero)
        self.single_digits_graph = single_digits_graph + pynini.closure(insert_space + single_digits_graph)

        if not deterministic:
            # for a single token allow only the same normalization
            # "007" -> {"oh oh seven", "zero zero seven"} not {"oh zero seven"}
            single_digits_graph_zero = pynini.invert(graph_digit | graph_zero)
            single_digits_graph_oh = pynini.invert(graph_digit) | pynini.cross("0", "oh")

            self.single_digits_graph = single_digits_graph_zero + pynini.closure(
                insert_space + single_digits_graph_zero
            )
            self.single_digits_graph |= single_digits_graph_oh + pynini.closure(insert_space + single_digits_graph_oh)

            single_digits_graph_with_commas = pynini.closure(
                self.single_digits_graph + insert_space, 1, 3
            ) + pynini.closure(
                pynutil.delete(",")
                + single_digits_graph
                + insert_space
                + single_digits_graph
                + insert_space
                + single_digits_graph,
                1,
            )

            self.range_graph = pynutil.insert("from ") + self.graph + pynini.cross("-", " to ") + self.graph
            self.range_graph |= self.graph + (pynini.cross("x", " by ") | pynini.cross(" x ", " by ")) + self.graph
            self.range_graph |= (
                pynutil.insert("from ") + get_hundreds_graph() + pynini.cross("-", " to ") + get_hundreds_graph()
            )
            self.range_graph = self.range_graph.optimize()

        serial_graph = self.get_serial_graph()
        optional_minus_graph = pynini.closure(pynutil.insert("negative: ") + pynini.cross("-", "\"true\" "), 0, 1)

        if deterministic:
            long_numbers = pynini.compose(NEMO_DIGIT ** (5, ...), self.single_digits_graph).optimize()
            final_graph = self.graph | serial_graph | pynutil.add_weight(long_numbers, -0.001)
            cardinal_with_leading_zeros = pynini.compose(
                pynini.accep("0") + pynini.closure(NEMO_DIGIT), self.single_digits_graph
            )
            final_graph |= cardinal_with_leading_zeros
        else:

            leading_zeros = pynini.compose(pynini.closure(pynini.accep("0"), 1), self.single_digits_graph)
            cardinal_with_leading_zeros = (
                leading_zeros + pynutil.insert(" ") + pynini.compose(pynini.closure(NEMO_DIGIT), self.graph)
            )

            # add small weight to non-default graphs to make sure the deterministic option is listed first
            final_graph = (
                self.graph
                | serial_graph
                | self.range_graph
                | pynutil.add_weight(self.single_digits_graph, 0.001)
                | pynutil.add_weight(get_hundreds_graph(), 0.001)
                | pynutil.add_weight(single_digits_graph_with_commas, 0.001)
                | cardinal_with_leading_zeros
            )

        final_graph = optional_minus_graph + pynutil.insert("integer: \"") + final_graph + pynutil.insert("\"")
        final_graph = self.add_tokens(final_graph)

        self.fst = final_graph.optimize()

    def get_serial_graph(self):
        """
        Finite state transducer for classifying serial (handles only cases without delimiters,
        values with delimiters are handled by default).
            The serial is a combination of digits, letters and dashes, e.g.:
            c325b -> tokens { cardinal { integer: "c three two five b" } }
        """
        num_graph = self.single_digits_graph

        if not self.deterministic:
            num_graph |= self.graph

        # add space between letter and digit
        graph_with_space = pynini.compose(
            pynini.cdrewrite(pynutil.insert(" "), NEMO_ALPHA, NEMO_DIGIT, NEMO_SIGMA),
            pynini.cdrewrite(pynutil.insert(" "), NEMO_DIGIT, NEMO_ALPHA, NEMO_SIGMA),
        )

        # make sure at least one digit and letter is present
        not_space = pynini.closure(NEMO_NOT_SPACE)
        graph_with_space = pynini.compose(
            (not_space + NEMO_ALPHA + not_space + NEMO_DIGIT + not_space)
            | (not_space + NEMO_DIGIT + not_space + NEMO_ALPHA + not_space),
            graph_with_space,
        )

        keep_space = pynini.accep(" ")
        serial_graph = pynini.compose(
            graph_with_space,
            pynini.closure(pynini.closure(NEMO_ALPHA, 1) + keep_space, 1)
            + num_graph
            + pynini.closure(keep_space + pynini.closure(NEMO_ALPHA) + pynini.closure(keep_space + num_graph, 0, 1)),
        )
        serial_graph |= pynini.compose(
            graph_with_space,
            num_graph
            + keep_space
            + pynini.closure(NEMO_ALPHA, 1)
            + pynini.closure(keep_space + num_graph + pynini.closure(keep_space + pynini.closure(NEMO_ALPHA), 0, 1)),
        )

        # serial graph with delimiter
        delimiter = pynini.accep("-") | pynini.accep("/")
        alphas = pynini.closure(NEMO_ALPHA, 1)
        letter_num = alphas + delimiter + num_graph
        num_letter = pynini.closure(num_graph + delimiter, 1) + alphas
        next_alpha_or_num = pynini.closure(delimiter + (alphas | num_graph))
        next_alpha_or_num |= pynini.closure(delimiter + num_graph + pynutil.insert(" ") + alphas)

        serial_graph |= letter_num + next_alpha_or_num
        serial_graph |= num_letter + next_alpha_or_num
        # numbers only with 2+ delimiters
        serial_graph |= (
            num_graph + delimiter + num_graph + delimiter + num_graph + pynini.closure(delimiter + num_graph)
        )
        return pynutil.add_weight(serial_graph, 2)

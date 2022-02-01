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
    GraphFst,
    delete_space,
    insert_space,
)
from nemo_text_processing.text_normalization.en.utils import get_abs_path

try:
    import pynini
    from pynini.lib import pynutil

    PYNINI_AVAILABLE = True
except (ModuleNotFoundError, ImportError):
    PYNINI_AVAILABLE = False


class TelephoneFst(GraphFst):
    """
    Finite state transducer for classifying telephone, which includes country code, number part and extension 
    country code optional: +*** 
    number part: ***-***-****, or (***) ***-****
    extension optional: 1-9999
    E.g 
    +1 123-123-5678-1 -> telephone { country_code: "one" number_part: "one two three, one two three, five six seven eight" extension: "one" }
    1-800-GO-U-HAUL -> telephone { country_code: "one" number_part: "one, eight hundred GO U HAUL" }
    Args:
        deterministic: if True will provide a single transduction option,
            for False multiple transduction are generated (used for audio-based normalization)
    """

    def __init__(self, deterministic: bool = True):
        super().__init__(name="telephone", kind="classify", deterministic=deterministic)

        add_separator = pynutil.insert(", ")  # between components
        digit = pynini.invert(pynini.string_file(get_abs_path("data/numbers/digit.tsv"))).optimize() | pynini.cross(
            "0", "o"
        )

        country_code = (
            pynutil.insert("country_code: \"")
            + pynini.closure(pynutil.delete("+"), 0, 1)
            + pynini.closure(digit + insert_space, 0, 2)
            + digit
            + pynutil.insert("\"")
        )
        optional_country_code = pynini.closure(
            country_code + pynini.closure(pynutil.delete("-"), 0, 1) + delete_space + insert_space, 0, 1
        )

        area_part_common = pynutil.add_weight(pynini.cross("800", "eight hundred"), -1.1)
        area_part_default = pynini.closure(digit + insert_space, 2, 2) + digit
        area_part = area_part_default | area_part_common

        area_part = (
            (area_part + pynutil.delete("-"))
            | (pynutil.delete("(") + area_part + (pynutil.delete(") ") | pynutil.delete(")-")))
        ) + add_separator

        del_separator = pynini.closure(pynini.union("-", " "), 0, 1)
        number_length = ((NEMO_DIGIT + del_separator) | (NEMO_ALPHA + del_separator)) ** 7
        number_words = pynini.closure(
            (NEMO_DIGIT @ digit) + (insert_space | pynini.cross("-", ', '))
            | NEMO_ALPHA
            | (NEMO_ALPHA + pynini.cross("-", ' '))
        )
        number_words = pynini.compose(number_length, number_words)
        number_part = area_part + number_words
        number_part = pynutil.insert("number_part: \"") + number_part + pynutil.insert("\"")
        extension = (
            pynutil.insert("extension: \"") + pynini.closure(digit + insert_space, 0, 3) + digit + pynutil.insert("\"")
        )
        optional_extension = pynini.closure(insert_space + extension, 0, 1)

        graph = optional_country_code + number_part + optional_extension

        # ip
        digit_to_str_graph = pynini.compose(
            NEMO_DIGIT ** (1, 3), digit + pynini.closure(pynutil.insert(" ") + digit)
        ).optimize()
        ip_graph = digit_to_str_graph + (pynini.cross(".", " dot ") + digit_to_str_graph) ** 3
        graph |= pynutil.insert("number_part: \"") + ip_graph.optimize() + pynutil.insert("\"")

        final_graph = self.add_tokens(graph)
        self.fst = final_graph.optimize()

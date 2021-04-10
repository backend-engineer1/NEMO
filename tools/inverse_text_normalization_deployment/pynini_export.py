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


import os
import sys

import pynini
from nemo_text_processing.inverse_text_normalization.taggers.tokenize_and_classify import ClassifyFst
from nemo_text_processing.inverse_text_normalization.verbalizers.verbalize import VerbalizeFst
from pynini.export import export

# This script exports OpenFst finite state archive files tokenize_and_classify_tmp.far and verbalize_tmp.far from compiled grammars inside NeMo inverse text normalization for  production purposes


def _generator_main(file_name: str, graph: pynini.FstLike, rule_name: str):
    """
    Exports graph as OpenFst finite state archive (FAR) file with given file name and rule name. 

    Args:
        file_name: exported file name
        graph: Pynini WFST graph to be exported
        rule_name: rule name for graph in created FAR file

    """
    exporter = export.Exporter(file_name)
    exporter[rule_name] = graph.optimize()
    exporter.close()
    print(f'Created {file_name}')


def export_grammars(output_dir):
    """
    Exports tokenizer_and_classify and verbalize Fsts as OpenFst finite state archive (FAR) files. 

    Args:
        output_dir: directory to export FAR files to. Subdirectories will be created for tagger and verbalizer respectively.
    """
    d = {}
    d['tokenize_and_classify'] = {'classify': ClassifyFst().fst}
    d['verbalize'] = {'verbalize': VerbalizeFst().fst}

    for category, graphs in d.items():
        for stage, fst in graphs.items():
            out_dir = os.path.join(output_dir, stage)
            os.makedirs(out_dir, exist_ok=True)
            _generator_main(f"{out_dir}/{category}_tmp.far", fst, category.upper())


if __name__ == '__main__':
    export_grammars(sys.argv[1])

# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

from argparse import ArgumentParser

from tools.text_normalization.normalize import normalizers
from tools.text_normalization.utils import (
    evaluate,
    known_types,
    load_files,
    training_data_to_sentences,
    training_data_to_tokens,
)


'''
Runs Evaluation on data in the format of : <semiotic class>\t<unnormalized text>\t<`self` if trivial class or normalized text>
like the Google text normalization data https://www.kaggle.com/richardwilliamsproat/text-normalization-for-english-russian-and-polish
'''


def parse_args():
    parser = ArgumentParser()
    parser.add_argument("--input", help="input file path", type=str)
    parser.add_argument(
        "--normalizer",
        default='nemo',
        help="normalizer to use (" + ", ".join(normalizers.keys()) + ")",
        type=str,
        choices=normalizers.keys(),
    )
    parser.add_argument(
        "--cat", dest="category", help="focus on class only (" + ", ".join(known_types) + ")", type=str, default=None
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    file_path = args.input
    normalizer = normalizers[args.normalizer]

    print("Loading training data: " + file_path)
    training_data = load_files([file_path])

    if args.category is None:
        print("Sentence level evaluation...")
        sentences_un_normalized, sentences_normalized = training_data_to_sentences(training_data)
        print("- Data: " + str(len(sentences_un_normalized)) + " sentences")
        sentences_prediction = normalizer(sentences_un_normalized)
        print("- Normalized. Evaluating...")
        sentences_accuracy = evaluate(sentences_prediction, sentences_normalized, sentences_un_normalized)
        print("- Accuracy: " + str(sentences_accuracy))

    print("Token level evaluation...")
    tokens_per_type = training_data_to_tokens(training_data, category=args.category)
    token_accuracy = {}
    for token_type in tokens_per_type:
        print("- Token type: " + token_type)
        tokens_un_normalized, tokens_normalized = tokens_per_type[token_type]
        print("  - Data: " + str(len(tokens_un_normalized)) + " tokens")
        tokens_prediction = normalizer(tokens_un_normalized)
        print("  - Normalized. Evaluating...")
        token_accuracy[token_type] = evaluate(tokens_prediction, tokens_normalized, tokens_un_normalized)
        print("  - Accuracy: " + str(token_accuracy[token_type]))
    token_count_per_type = {token_type: len(tokens_per_type[token_type][0]) for token_type in tokens_per_type}
    token_weighted_accuracy = [
        token_count_per_type[token_type] * accuracy for token_type, accuracy in token_accuracy.items()
    ]
    print("- Accuracy: " + str(sum(token_weighted_accuracy) / sum(token_count_per_type.values())))

    # csv output
    for token_type in token_accuracy:
        if token_type not in known_types:
            raise ValueError("Unexpected token type: " + token_type)
    print('')
    print('\tsentence level\ttoken level')
    print('\t\t' + '\t'.join(known_types))

    if args.category is None:
        print(
            'numbers\t'
            + str(len(sentences_un_normalized))
            + '\t'
            + '\t'.join([str(token_count_per_type[known_type]) for known_type in known_types])
        )
        print(
            args.normalizer
            + '\t'
            + str(sentences_accuracy)
            + '\t'
            + '\t'.join([str(token_accuracy[known_type]) for known_type in known_types])
        )
    else:
        print('numbers\t' + '\t'.join([str(token_count_per_type[known_type]) for known_type in known_types]))
        print(args.normalizer + '\t'.join([str(token_accuracy[known_type]) for known_type in known_types]))

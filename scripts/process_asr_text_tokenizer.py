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

# USAGE: python process_asr_text_tokenizer.py --manifest=<path to train manifest files, seperated by commas> \
#         --data_root="<output directory>" \
#         --vocab_size=<number of tokens in vocabulary> \
#         --tokenizer=<"bpe" or "wpe"> \
#         --log
# where <manifest> can be: train_clean_100, train_clean_360, train_other_500
# You can also put more than one data_set comma-separated:
# --manifest="train_clean_100,train_clean_360,train_other_500"
# or
#       python process_asr_text_tokenizer.py --data_file=<path to train text file> \
#         --data_root="<output directory>" \
#         --vocab_size=<number of tokens in vocabulary> \
#         --tokenizer=<"bpe" or "wpe"> \
#         --log
# where <manifest> can be: train_clean_100, train_clean_360, train_other_500
# You can also put more than one data_set comma-separated:
# --manifest="train_clean_100,train_clean_360,train_other_500"
import argparse
import json
import logging
import os

import tokenizers

from nemo.collections.common.tokenizers.sentencepiece_tokenizer import create_spt_model

parser = argparse.ArgumentParser(description='Create tokenizer')
group = parser.add_mutually_exclusive_group(required=True)
group.add_argument("--manifest", default=None, type=str, help='Comma separated list of manifest files')
group.add_argument("--data_file", default=None, help='data file from which to create tokenizer model')
parser.add_argument("--data_root", required=True, default=None, type=str, help='Output directory')
parser.add_argument("--vocab_size", default=1024, type=int, help='Vocabulary size')
parser.add_argument("--tokenizer", default="wpe", choices=["spe", "wpe"], help='Type of tokenization to perform')
parser.add_argument(
    "--spe_type",
    default="bpe",
    choices=['bpe', 'unigram', 'char', 'word'],
    help='Type of the SentencePiece model. Can be `bpe`, `unigram`, `char` or `word`.'
    'Used only if --tokenizer == `spe`',
)
parser.add_argument("--log", action='store_true')
parser.set_defaults(log=False)
args = parser.parse_args()


def __build_document_from_manifests(
    data_root: str, manifests: str,
):
    if ',' in manifests:
        manifests = manifests.split(',')
    else:
        manifests = [manifests]

    document_dir = os.path.join(data_root, 'text_corpus')
    if not os.path.exists(document_dir):
        os.makedirs(document_dir)

    document_path = os.path.join(document_dir, 'document.txt')

    if os.path.exists(document_path):
        logging.info('Corpus already exists at path : %s', document_path)
        return document_path

    num_lines = 0
    with open(document_path, 'w') as out_writer:
        for manifest in manifests:
            with open(manifest, 'r') as in_reader:
                for line in in_reader:
                    item = json.loads(line)
                    text = item['text']

                    out_writer.write(text + '\n')
                    out_writer.flush()

                    num_lines += 1

            logging.info(f"Finished extracting manifest : {manifest}")

        logging.info("Finished extracting all manifests ! Number of sentences : {}".format(num_lines))
    return document_path


def __process_data(text_path: str, dst_folder: str, vocab_size: int, tokenizer_type: str, spe_type: str):
    """
    Converts flac to wav and build manifests's json
    Args:
        text_path: source with text lines
        dst_folder: where wav files will be stored
        vocab_size: vocabular size used in encoding the text
        tokenizer_type: type of tokenization to perform - wpe or spe
        spe_type: type of tokenization model used for spe.

    Returns:
    """
    tokenizer_dir = os.path.join(dst_folder, 'tokenizer_{}_v{}').format(tokenizer_type, vocab_size)

    if not os.path.exists(tokenizer_dir):
        os.makedirs(tokenizer_dir)

    if tokenizer_type == 'spe':
        if os.path.exists(os.path.join(tokenizer_dir, 'tokenizer.model')):
            logging.warning("Model file already exists, overriding old model file !")
            os.remove(os.path.join(tokenizer_dir, 'tokenizer.model'))

        tokenizer_path, vocab_path = create_spt_model(
            data_file=text_path,
            vocab_size=vocab_size,
            sample_size=-1,
            do_lower_case=True,
            output_dir=tokenizer_dir,
            tokenizer_type=spe_type,
        )

    else:
        tokenizer = tokenizers.BertWordPieceTokenizer(lowercase=True)

        tokenizer.train(text_path, vocab_size=vocab_size)
        tokenizer.save_model(tokenizer_dir)

    return tokenizer_dir


def main():
    data_root = args.data_root
    manifests = args.manifest
    data_file = args.data_file
    vocab_size = args.vocab_size
    tokenizer = args.tokenizer
    spe_type = args.spe_type

    if not os.path.exists(data_root):
        os.makedirs(data_root)

    if args.log:
        logging.basicConfig(level=logging.INFO)

    if manifests:
        text_corpus_path = __build_document_from_manifests(data_root, manifests)
    else:
        text_corpus_path = data_file
    tokenizer_path = __process_data(text_corpus_path, data_root, vocab_size, tokenizer, spe_type)

    print("Serialized tokenizer at location :", tokenizer_path)
    logging.info('Done!')


if __name__ == "__main__":
    main()

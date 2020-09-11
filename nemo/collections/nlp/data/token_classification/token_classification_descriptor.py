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

import os
from typing import List

from nemo.collections.nlp.data.data_utils.data_preprocessing import (
    fill_class_weights,
    get_freq_weights,
    get_label_stats,
)
from nemo.utils import logging

__all__ = ['TokenClassificationDataDesc']


class TokenClassificationDataDesc:
    def __init__(self, data_dir: str, modes: List[str] = ['train', 'test', 'dev'], pad_label='O', label_ids_dict=None):
        """A descriptor class that reads all the data and calculates some stats of the data and also calculates
        the class weights to be used for class balancing
        Args:
            data_dir: the path to the data folder
            modes: list of the modes to read, it can be from ["train", "test", "dev"] by default.
            It is going to look for the data files at {data_dir}/{mode}.txt
            label_ids_dict: labels to ids mapping from pretrained model
        """
        self.data_dir = data_dir
        self.label_ids = None
        unique_labels = set()

        for mode in modes:
            all_labels = []
            label_file = os.path.join(data_dir, 'labels_' + mode + '.txt')
            if not os.path.exists(label_file):
                logging.info(f'Stats calculation for {mode} mode is skipped as {label_file} was not found.')
                continue

            with open(label_file, 'r') as f:
                for line in f:
                    line = line.strip().split()
                    all_labels.extend(line)
                    unique_labels.update(line)

            if mode == 'train':
                label_ids = {pad_label: 0}
                if pad_label in unique_labels:
                    unique_labels.remove(pad_label)
                for label in sorted(unique_labels):
                    label_ids[label] = len(label_ids)

                self.pad_label = pad_label
                if label_ids_dict:
                    if len(set(label_ids_dict) | set(label_ids)) != len(label_ids_dict):
                        raise ValueError(
                            f'Provided labels to ids map: {label_ids_dict} does not match the labels '
                            f'in the data: {label_ids}'
                        )
                self.label_ids = label_ids_dict if label_ids_dict else label_ids
                logging.info(f'Labels: {self.label_ids}')
                self.label_ids_filename = os.path.join(data_dir, 'label_ids.csv')
                out = open(self.label_ids_filename, 'w')
                labels, _ = zip(*sorted(self.label_ids.items(), key=lambda x: x[1]))
                out.write('\n'.join(labels))
                logging.info(f'Labels mapping saved to : {out.name}')

            all_labels = [self.label_ids[label] for label in all_labels]
            logging.info(f'Three most popular labels in {mode} dataset:')
            total_labels, label_frequencies, max_id = get_label_stats(
                all_labels, os.path.join(data_dir, mode + '_label_stats.tsv')
            )

            logging.info(f'Total labels: {total_labels}')
            logging.info(f'Label frequencies - {label_frequencies}')

            if mode == 'train':
                class_weights_dict = get_freq_weights(label_frequencies)
                logging.info(f'Class Weights: {class_weights_dict}')
                self.class_weights = fill_class_weights(class_weights_dict, max_id)
                self.num_classes = max_id + 1

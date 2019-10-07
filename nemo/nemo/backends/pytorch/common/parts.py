__all__ = ['TextDataset', 'Attention', 'MultiLayerPerceptron']

import os

import pandas as pd
import torch
# noinspection PyPep8Naming
from torch import nn
from torch.utils.data import Dataset


class TextDataset(Dataset):
    def __init__(self, path, labels, eos_id):
        _, ext = os.path.splitext(path)
        if ext == '.csv':
            texts = pd.read_csv(path)['transcript'].tolist()
        else:
            with open(path, 'r') as f:
                texts = f.readlines()
        texts = [l.strip().lower() for l in texts if len(l)]
        self.texts = texts

        self.char2num = {c: i for i, c in enumerate(labels)}
        self.eos_id = eos_id

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, item):
        char2num = self.char2num
        return torch.tensor(
            [char2num[c] for c in self.texts[item]
             if c in char2num] + [self.eos_id],
            dtype=torch.long
        )


class Attention(nn.Module):
    def __init__(self, dims, method='general', dropout=0.0):
        super().__init__()

        if method not in ('dot', 'general'):
            raise ValueError("Invalid attention type selected")

        self.method = method

        if method == 'general':
            self.linear_in = nn.Linear(dims, dims, bias=False)
        self.dropout = nn.Dropout(dropout)
        self.linear_out = nn.Linear(dims * 2, dims, bias=False)
        self.softmax = nn.Softmax(dim=-1)
        self.tanh = nn.Tanh()

    def forward(self, query, context):
        batch_size, output_len, dims = query.size()
        query_len = context.size(1)

        if self.method == 'general':
            query = query.contiguous()  # Cant call `.view` w/o it
            query = query.view(batch_size * output_len, dims)
            query = self.linear_in(query)
            query = query.view(batch_size, output_len, dims)

        attention_scores = torch.bmm(
            query, context.transpose(1, 2).contiguous()
        )

        attention_scores = attention_scores.view(
            batch_size * output_len, query_len
        )
        attention_weights = self.softmax(attention_scores)
        if self.dropout.p != 0.0:
            attention_weights = self.dropout(attention_weights)
        attention_weights = attention_weights.view(
            batch_size, output_len, query_len
        )

        mix = torch.bmm(attention_weights, context)

        combined = torch.cat((mix, query), dim=2)
        combined = combined.view(batch_size * output_len, 2 * dims)

        output = self.linear_out(combined).view(batch_size, output_len, dims)
        output = self.tanh(output)

        return output, attention_weights


class MultiLayerPerceptron(nn.Module):
    """
    A simple MLP that can either be used independently or put on top
    of pretrained models (such as BERT) and act as a classifier.

    Args:
        hidden_size (int): the size of each layer
        num_classes (int): number of output classes
        device: whether it's CPU or CUDA
        num_layers (int): number of layers
        activation: type of activations for layers in between
        log_softmax (bool): whether to add a log_softmax layer before output
    """

    def __init__(self,
                 hidden_size,
                 num_classes,
                 device,
                 num_layers=2,
                 activation='relu',
                 log_softmax=True):
        super().__init__()

        self.layers = []
        for _ in range(num_layers - 1):
            self.layers.append(nn.Linear(hidden_size, hidden_size).to(device))
            self.layers.append(getattr(torch, activation))

        self.layers.append(nn.Linear(hidden_size, num_classes).to(device))
        self.log_softmax = log_softmax

    def forward(self, hidden_states):
        output_states = hidden_states
        for layer in self.layers:
            output_states = layer(output_states)

        if self.log_softmax:
            output_states = torch.log_softmax(
                output_states.float(), dim=-1).to(hidden_states.dtype)
            # TODO: make it work with float16
        return output_states

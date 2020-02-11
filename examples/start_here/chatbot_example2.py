# Copyright (c) 2019 NVIDIA Corporation
import gzip
import os
import shutil

import nemo

logging = nemo.logging

# Get Data
data_file = "movie_data.txt"
if not os.path.isfile(data_file):
    with gzip.open("../../tests/data/movie_lines.txt.gz", 'rb') as f_in:
        with open(data_file, 'wb') as f_out:
            shutil.copyfileobj(f_in, f_out)

# instantiate Neural Factory with supported backend
neural_factory = nemo.core.NeuralModuleFactory()

# instantiate necessary neural modules
dl = nemo.tutorials.DialogDataLayer(batch_size=128, corpus_name="cornell", datafile=data_file)

# Instance one on EncoderRNN
encoder1 = nemo.tutorials.EncoderRNN(voc_size=(6104 + 3), encoder_n_layers=2, hidden_size=512, dropout=0.1)

# Instance two on EncoderRNN. It will have different weights from instance one
encoder2 = nemo.tutorials.EncoderRNN(voc_size=(6104 + 3), encoder_n_layers=2, hidden_size=512, dropout=0.1)

# Create a simple combiner mixing the encodings.
mixer = nemo.backends.pytorch.common.SimpleCombiner()

decoder = nemo.tutorials.LuongAttnDecoderRNN(
    attn_model="dot", hidden_size=512, voc_size=(6104 + 3), decoder_n_layers=2, dropout=0.1
)

L = nemo.tutorials.MaskedXEntropyLoss()

decoderInfer = nemo.tutorials.GreedyLuongAttnDecoderRNN(
    attn_model="dot", hidden_size=512, voc_size=(6104 + 3), decoder_n_layers=2, dropout=0.1, max_dec_steps=10
)

# notice trainng and inference decoder share parameters
decoderInfer.tie_weights_with(decoder, list(decoder.get_weights().keys()))

# express activations flow
src, src_lengths, tgt, mask, max_tgt_length = dl()
encoder_outputs1, encoder_hidden1 = encoder1(input_seq=src, input_lengths=src_lengths)
encoder_outputs2, encoder_hidden2 = encoder2(input_seq=src, input_lengths=src_lengths)
encoder_outputs = mixer(x1=encoder_outputs1, x2=encoder_outputs2)
outputs, hidden = decoder(targets=tgt, encoder_outputs=encoder_outputs, max_target_len=max_tgt_length)
loss = L(predictions=outputs, target=tgt, mask=mask)

# run inference decoder to generate predictions
outputs_inf, _ = decoderInfer(encoder_outputs=encoder_outputs)


# this function is necessary to print intermediate results to console
def outputs2words(tensors, vocab):
    source_ids = tensors[1][:, 0].cpu().numpy().tolist()
    response_ids = tensors[2][:, 0].cpu().numpy().tolist()
    tgt_ids = tensors[3][:, 0].cpu().numpy().tolist()
    source = list(map(lambda x: vocab[x], source_ids))
    response = list(map(lambda x: vocab[x], response_ids))
    target = list(map(lambda x: vocab[x], tgt_ids))
    source = ' '.join([s for s in source if s != 'EOS' and s != 'PAD'])
    response = ' '.join([s for s in response if s != 'EOS' and s != 'PAD'])
    target = ' '.join([s for s in target if s != 'EOS' and s != 'PAD'])
    logging.info(f'Train Loss: {str(tensors[0].item())}')
    tmp = " SOURCE: {0} <---> PREDICTED RESPONSE: {1} <---> TARGET: {2}"
    return tmp.format(source, response, target)


# Create trainer and execute training action
callback = nemo.core.SimpleLossLoggerCallback(
    tensors=[loss, src, outputs_inf, tgt], print_func=lambda x: outputs2words(x, dl.voc.index2word),
)
# Instantiate an optimizer to perform `train` action
optimizer = neural_factory.get_trainer()

optimizer.train(
    tensors_to_optimize=[loss],
    callbacks=[callback],
    optimizer="adam",
    optimization_params={"num_epochs": 15, "lr": 0.001},
)

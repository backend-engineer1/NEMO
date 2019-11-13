# Copyright (c) 2019 NVIDIA Corporation
import argparse
import copy
import os

import librosa
import matplotlib.pyplot as plt
import numpy as np
from ruamel.yaml import YAML
from scipy.io.wavfile import write

import nemo
import nemo_asr
import nemo_tts
from tacotron2 import create_NMs


def parse_args():
    parser = argparse.ArgumentParser(description='TTS')
    parser.add_argument(
        "--spec_model", type=str, required=True,
        choices=["tacotron2"],
        help="Model generated to generate spectrograms")
    parser.add_argument(
        "--vocoder", type=str, required=True,
        choices=["griffin-lim", "waveglow"],
        help="Vocoder used to convert from spectrograms to audio")
    parser.add_argument(
        "--spec_model_config", type=str, required=True,
        help="spec model configuration file: model.yaml")
    parser.add_argument(
        "--vocoder_model_config", type=str,
        help=("vocoder model configuration file: model.yaml. Not required for "
              "griffin-lim."))
    parser.add_argument(
        "--spec_model_load_dir", type=str, required=True,
        help="directory containing checkpoints for spec model")
    parser.add_argument(
        "--vocoder_model_load_dir", type=str,
        help=("directory containing checkpoints for vocoder model. Not "
              "required for griffin-lim"))
    parser.add_argument("--eval_dataset", type=str, required=True)
    parser.add_argument(
        "--save_dir", type=str,
        help="directory to save audio files to")
    parser.add_argument(
        "--disable_denoiser", action="store_true",
        help="pass flag to avoid denoiser step in waveglow")

    args = parser.parse_args()
    if (args.vocoder == "griffin-lim" and
            (args.vocoder_model_config or args.vocoder_model_load_dir)):
        raise ValueError(
            "Griffin-Lim was specified as the vocoder but the a value for "
            "vocoder_model_config or vocoder_model_load_dir was passed.")
    return args


def griffin_lim(magnitudes, n_iters=50, n_fft=1024):
    """
    Griffin-Lim algorithm to convert magnitude spectrograms to audio signals
    """
    phase = np.exp(2j * np.pi * np.random.rand(*magnitudes.shape))
    complex_spec = magnitudes * phase
    signal = librosa.istft(complex_spec)
    if not np.isfinite(signal).all():
        print("WARNING: audio was not finite, skipping audio saving")
        return np.array([0])

    for _ in range(n_iters):
        _, phase = librosa.magphase(librosa.stft(signal, n_fft=n_fft))
        complex_spec = magnitudes * phase
        signal = librosa.istft(complex_spec)
    return signal


def plot_and_save_spec(spectrogram, i, save_dir=None):
    fig, ax = plt.subplots(figsize=(12, 3))
    im = ax.imshow(spectrogram, aspect="auto", origin="lower",
                   interpolation='none')
    plt.colorbar(im, ax=ax)
    plt.xlabel("Frames")
    plt.ylabel("Channels")
    plt.tight_layout()
    save_file = f"spec_{i}.png"
    if save_dir:
        save_file = os.path.join(save_dir, save_file)
    plt.savefig(save_file)
    plt.close()


def create_infer_dags(neural_factory,
                      neural_modules,
                      tacotron2_params,
                      infer_dataset,
                      infer_batch_size,
                      cpu_per_dl=1):
    (_, text_embedding, t2_enc, t2_dec, t2_postnet, _, _) = neural_modules

    dl_params = copy.deepcopy(tacotron2_params["AudioToTextDataLayer"])
    dl_params.update(tacotron2_params["AudioToTextDataLayer"]["eval"])
    del dl_params["train"]
    del dl_params["eval"]

    data_layer = nemo_asr.AudioToTextDataLayer(
        manifest_filepath=infer_dataset,
        labels=tacotron2_params['labels'],
        batch_size=infer_batch_size,
        num_workers=cpu_per_dl,
        load_audio=False,
        **dl_params,
    )

    _, _, transcript, transcript_len = data_layer()

    transcript_embedded = text_embedding(char_phone=transcript)
    transcript_encoded = t2_enc(
        char_phone_embeddings=transcript_embedded,
        embedding_length=transcript_len)
    if isinstance(t2_dec, nemo_tts.Tacotron2DecoderInfer):
        mel_decoder, gate, alignments, mel_len = t2_dec(
            char_phone_encoded=transcript_encoded,
            encoded_length=transcript_len)
    else:
        raise ValueError(
            "The Neural Module for tacotron2 decoder was not understood")
    mel_postnet = t2_postnet(mel_input=mel_decoder)

    return [mel_postnet, gate, alignments, mel_len]


def main():
    args = parse_args()
    neural_factory = nemo.core.NeuralModuleFactory(
        backend=nemo.core.Backend.PyTorch)

    # Create text to spectrogram model
    if args.spec_model == "tacotron2":
        yaml = YAML(typ="safe")
        with open(args.spec_model_config) as file:
            tacotron2_params = yaml.load(file)
        spec_neural_modules = create_NMs(tacotron2_params, decoder_infer=True)
        infer_tensors = create_infer_dags(
            neural_factory=neural_factory,
            neural_modules=spec_neural_modules,
            tacotron2_params=tacotron2_params,
            infer_dataset=args.eval_dataset,
            infer_batch_size=32)

    print("Running Tacotron 2")
    # Run tacotron 2
    evaluated_tensors = neural_factory.infer(
        tensors=infer_tensors,
        checkpoint_dir=args.spec_model_load_dir,
        cache=True,
        offload_to_cpu=False
    )
    mel_len = evaluated_tensors[-1]
    print("Done Running Tacotron 2")
    filterbank = librosa.filters.mel(22050, 1024, n_mels=80, fmin=0,
                                     fmax=8000)

    if args.vocoder == "griffin-lim":
        print("Running Griffin-Lim")
        mel_spec = evaluated_tensors[2]
        for i, batch in enumerate(mel_spec):
            log_mel = batch.cpu().numpy().transpose(0, 2, 1)
            mel = np.exp(log_mel)
            magnitudes = np.dot(mel, filterbank) * 2048
            for j, sample in enumerate(magnitudes):
                sample = sample[:mel_len[i][j], :]
                audio = griffin_lim(sample.T ** 1.2)
                save_file = f"sample_{i*32+j}.wav"
                if args.save_dir:
                    save_file = os.path.join(args.save_dir, save_file)
                write(save_file, 22050, audio)
                plot_and_save_spec(log_mel[j][:mel_len[i][j], :].T, i*32+j,
                                   args.save_dir)

    elif args.vocoder == "waveglow":
        (mel_pred, _, _, _) = infer_tensors
        if not args.vocoder_model_config or not args.vocoder_model_load_dir:
            raise ValueError(
                "Using waveglow as the vocoder requires the "
                "--vocoder_model_config and --vocoder_model_load_dir args")

        yaml = YAML(typ="safe")
        with open(args.vocoder_model_config) as file:
            waveglow_params = yaml.load(file)
        waveglow = nemo_tts.WaveGlowInferNM(**waveglow_params["WaveGlowNM"])
        audio_pred = waveglow(mel_spectrogram=mel_pred)

        # Run waveglow
        print("Running Waveglow")
        evaluated_tensors = neural_factory.infer(
            tensors=[audio_pred],
            checkpoint_dir=args.vocoder_model_load_dir,
            modules_to_restore=[waveglow],
            use_cache=True
        )
        print("Done Running Waveglow")

        if not args.disable_denoiser:
            print("Setup denoiser")
            waveglow.setup_denoiser()

        print("Saving results to disk")
        for i, batch in enumerate(evaluated_tensors[0]):
            audio = batch.cpu().numpy()
            for j, sample in enumerate(audio):
                sample = sample[:mel_len[i][j] * 256]
                save_file = f"sample_{i*32+j}.wav"
                if args.save_dir:
                    save_file = os.path.join(args.save_dir, save_file)
                if not args.disable_denoiser:
                    sample, spec = waveglow.denoise(sample, strength=0.1)
                else:
                    spec, _ = librosa.core.magphase(librosa.core.stft(
                        sample, n_fft=1024))
                write(save_file, 22050, sample)
                spec = np.dot(filterbank, spec)
                spec = np.log(np.clip(spec, a_min=1e-5, a_max=None))
                plot_and_save_spec(spec, i*32+j, args.save_dir)


if __name__ == '__main__':
    main()

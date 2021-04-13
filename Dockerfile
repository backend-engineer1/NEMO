# syntax=docker/dockerfile:experimental

# Copyright (c) 2019, NVIDIA CORPORATION. All rights reserved.
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

ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:21.03-py3


# build an image that includes only the nemo dependencies, ensures that dependencies
# are included first for optimal caching, and useful for building a development
# image (by specifying build target as `nemo-deps`)
FROM ${BASE_IMAGE} as nemo-deps

# Ensure apt-get won't prompt for selecting options
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && \
    apt-get install -y \
    libsndfile1 sox \
    libfreetype6 \
    python-setuptools swig \
    python-dev ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# uninstall stuff from base container
RUN pip uninstall -y sacrebleu torchtext

# build torchaudio (change latest release version to match pytorch)
WORKDIR /tmp/torchaudio_build
RUN git clone --depth 1 --branch release/0.7 https://github.com/pytorch/audio.git && \
    cd audio && \
    BUILD_SOX=1 python setup.py install && \
    cd .. && rm -r audio

# TODO: remove when 21.04 container is released
# build torchtext
WORKDIR /tmp/torchtext_build
RUN git clone --branch v0.8.1 https://github.com/pytorch/text.git && \
    cd text && \
    git submodule update --init --recursive && \
    python setup.py clean install && \
    cd .. && rm -r text

#install TRT tools: PT quantization support and ONNX graph optimizer
WORKDIR /tmp/trt_build
RUN git clone https://github.com/NVIDIA/TensorRT.git && \
    cd TensorRT/tools/onnx-graphsurgeon && python setup.py install && \
    cd ../pytorch-quantization && \
    python setup.py install && \
    rm -fr  /tmp/trt_build

# install nemo dependencies
WORKDIR /tmp/nemo
COPY requirements .
RUN for f in $(ls requirements*.txt); do pip install --disable-pip-version-check --no-cache-dir -r $f; done

# install nemo_text_processing dependencies
COPY nemo_text_processing /tmp/nemo/nemo_text_processing/
RUN /bin/bash /tmp/nemo/nemo_text_processing/setup.sh

# copy nemo source into a scratch image
FROM scratch as nemo-src
COPY . .

# start building the final container
FROM nemo-deps as nemo
ARG NEMO_VERSION=1.0.0rc1

# Check that NEMO_VERSION is set. Build will fail without this. Expose NEMO and base container
# version information as runtime environment variable for introspection purposes
RUN /usr/bin/test -n "$NEMO_VERSION" && \
    /bin/echo "export NEMO_VERSION=${NEMO_VERSION}" >> /root/.bashrc && \
    /bin/echo "export BASE_IMAGE=${BASE_IMAGE}" >> /root/.bashrc
RUN --mount=from=nemo-src,target=/tmp/nemo cd /tmp/nemo && pip install ".[all]" && \
    python -c "import nemo.collections.asr as nemo_asr" && \
    python -c "import nemo.collections.nlp as nemo_nlp" && \
    python -c "import nemo.collections.tts as nemo_tts" && \
    python -c "import nemo_text_processing.text_normalization as text_normalization"

# TODO: Remove once 21.04 container is base container
# install latest numba version
RUN conda update -c numba numba -y

# copy scripts/examples/tests into container for end user
WORKDIR /workspace/nemo
COPY scripts /workspace/nemo/scripts
COPY examples /workspace/nemo/examples
COPY tests /workspace/nemo/tests
COPY tutorials /workspace/nemo/tutorials
# COPY README.rst LICENSE /workspace/nemo/

RUN printf "#!/bin/bash\njupyter lab --no-browser --allow-root --ip=0.0.0.0" >> start-jupyter.sh && \
    chmod +x start-jupyter.sh


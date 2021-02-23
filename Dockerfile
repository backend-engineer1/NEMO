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

ARG BASE_IMAGE=nvcr.io/nvidia/pytorch:20.11-py3


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

# build torchaudio (change latest release version to match pytorch)
WORKDIR /tmp/torchaudio_build
RUN git clone --depth 1 --branch release/0.6 https://github.com/pytorch/audio.git && \
    cd audio && \
    BUILD_SOX=1 python setup.py install && \
    cd .. && rm -r audio

# build RNN-T loss
WORKDIR /workspace/deps/rnnt
RUN COMMIT_SHA=f546575109111c455354861a0567c8aa794208a2 && \
    git clone https://github.com/HawkAaron/warp-transducer && \
    cd warp-transducer && \
    git checkout $COMMIT_SHA && \
    # disable old compile flags (compute_30 arch)
    sed -i 's/set(CUDA_NVCC_FLAGS "${CUDA_NVCC_FLAGS} -gencode arch=compute_30,code=sm_30 -O2")/#set(CUDA_NVCC_FLAGS "${CUDA_NVCC_FLAGS} -gencode arch=compute_30,code=sm_30 -O2")/g' CMakeLists.txt && \
    # enable Cuda 11 compilation if necessary
    sed -i 's/set(CUDA_NVCC_FLAGS "${CUDA_NVCC_FLAGS} -gencode arch=compute_75,code=sm_75")/set(CUDA_NVCC_FLAGS "${CUDA_NVCC_FLAGS} -gencode arch=compute_80,code=sm_80")/g' CMakeLists.txt && \
    # build loss function
    mkdir build && \
    cd build && \
    cmake .. && \
    make VERBOSE=1 && \
    # set env flags
    export CUDA_HOME="/usr/local/cuda" && \
    export WARP_RNNT_PATH=`pwd` && \
    export CUDA_TOOLKIT_ROOT_DIR=$CUDA_HOME && \
    export LD_LIBRARY_PATH="$CUDA_HOME/extras/CUPTI/lib64:$LD_LIBRARY_PATH" && \
    export LIBRARY_PATH=$CUDA_HOME/lib64:$LIBRARY_PATH && \
    export LD_LIBRARY_PATH=$CUDA_HOME/lib64:$LD_LIBRARY_PATH && \
    export CFLAGS="-I$CUDA_HOME/include $CFLAGS" && \
    # install pytorch binding
    cd ../pytorch_binding && \
    python3 setup.py install && \
    rm -rf ../tests test ../tensorflow_binding

# uninstall stuff from base container
RUN pip uninstall -y sacrebleu

# install nemo dependencies
WORKDIR /tmp/nemo
COPY requirements .
RUN for f in $(ls requirements/*.txt); do pip install --disable-pip-version-check --no-cache-dir -r $f; done

#install TRT tools: PT quantization support and ONNX graph optimizer
WORKDIR /tmp/trt_build
RUN git clone https://github.com/NVIDIA/TensorRT.git && \
    cd TensorRT/tools/onnx-graphsurgeon && python setup.py install && \
    cd ../pytorch-quantization && \
    python setup.py install && \
    rm -fr  /tmp/trt_build

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
RUN --mount=from=nemo-src,target=/tmp/nemo cd /tmp/nemo && pip install ".[all]"

# copy scripts/examples/tests into container for end user
WORKDIR /workspace/nemo
COPY scripts /workspace/nemo/scripts
COPY examples /workspace/nemo/examples
COPY tests /workspace/nemo/tests
COPY tutorials /workspace/nemo/tutorials
# COPY README.rst LICENSE /workspace/nemo/

RUN printf "#!/bin/bash\njupyter lab --no-browser --allow-root --ip=0.0.0.0" >> start-jupyter.sh && \
    chmod +x start-jupyter.sh


#!/usr/bin/env bash
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
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

# Use this script to install KenLM, OpenSeq2Seq decoder, Flashlight decoder
NEMO_PATH=/workspace/nemo  # Path to NeMo folder: /workspace/nemo if you use NeMo/Dockerfile
if [ "$#" -eq 1 ]
then
  NEMO_PATH=$1
fi
KENLM_MAX_ORDER=10 # Maximum order of KenLM model, also specified in the setup_os2s_decoders.py

cd $NEMO_PATH
apt-get update && apt-get upgrade -y && apt-get install -y liblzma-dev && rm -rf /var/lib/apt/lists/* # needed for flashlight decoder

git clone https://github.com/NVIDIA/OpenSeq2Seq
cd OpenSeq2Seq
git checkout ctc-decoders
cd ..
mv OpenSeq2Seq/decoders .
rm -rf OpenSeq2Seq
cd decoders
# patch setup code to support the recent distutils
sed -i 's/, distutils/, distutils\nimport distutils.ccompiler/g' setup.py

cp $NEMO_PATH/scripts/installers/setup_os2s_decoders.py ./setup.py
./setup.sh

# install Boost package for KenLM
wget https://boostorg.jfrog.io/artifactory/main/release/1.80.0/source/boost_1_80_0.tar.bz2 --no-check-certificate && tar --bzip2 -xf $NEMO_PATH/decoders/boost_1_80_0.tar.bz2 && cd boost_1_80_0 && ./bootstrap.sh && ./b2 --layout=tagged link=static,shared threading=multi,single install -j4 || echo FAILURE
export BOOST_ROOT=$NEMO_PATH/decoders/boost_1_80_0

# install KenLM
cd $NEMO_PATH/decoders/kenlm/build && cmake -DKENLM_MAX_ORDER=$KENLM_MAX_ORDER .. && make -j2
cd $NEMO_PATH/decoders/kenlm
python setup.py install --max_order=$KENLM_MAX_ORDER
export KENLM_LIB=$NEMO_PATH/decoders/kenlm/build/bin
export KENLM_ROOT=$NEMO_PATH/decoders/kenlm
cd ..

# install Flashlight
git clone https://github.com/flashlight/text && cd text
python setup.py bdist_wheel
pip install dist/*.whl
cd ..

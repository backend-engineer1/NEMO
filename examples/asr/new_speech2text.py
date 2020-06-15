import pytorch_lightning as pl
from ruamel.yaml import YAML

from nemo.collections.asr.models.asrconvctcmodel2 import ASRConvCTCModel

# from nemo.collections.asr.models.asrconvctcmodel2 import QuartxzNet

# Load model definition
yaml = YAML(typ="safe")
with open('/Users/okuchaiev/repos/NeMo/examples/asr/configs/jasper_an4-2.yaml') as f:
    model_config = yaml.load(f)

asr_model = ASRConvCTCModel(
    preprocessor_params=model_config['AudioToMelSpectrogramPreprocessor'],
    encoder_params=model_config['JasperEncoder'],
    decoder_params=model_config['JasperDecoder'],
)

# Setup where your training data is
asr_model.setup_training_data(model_config['AudioToTextDataLayer'])

trainer = pl.Trainer()
trainer.fit(asr_model)

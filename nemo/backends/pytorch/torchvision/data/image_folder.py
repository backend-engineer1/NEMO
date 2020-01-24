# Copyright (c) 2019 NVIDIA Corporation
from torchvision import transforms, datasets

from ...nm import DataLayerNM
from .....core import *


class ImageFolderDataLayer(DataLayerNM):
    """This class wraps Torchvision's ImageFolder data set API into
    NeuralModule."""

    @property
    def output_ports(self):
        """Returns definitions of module output ports.

        image:
            0: AxisType(BatchTag)

            1: AxisType(ChannelTag)

            2: AxisType(HeightTag, input_size)

            3: AxisType(WidthTag, input_size)


        label:
            0: AxisType(BatchTag)
        """
        return {
            "image": NeuralType(
                {
                    0: AxisType(BatchTag),
                    1: AxisType(ChannelTag),
                    2: AxisType(HeightTag, self._input_size),
                    3: AxisType(WidthTag, self._input_size),
                }
            ),
            "label": NeuralType({0: AxisType(BatchTag)}),
        }

    def __init__(self, *, input_size=32, batch_size, path, shuffle=True,
                 is_eval=False, **kwargs):
        DataLayerNM.__init__(self, **kwargs)

        self._input_size = input_size
        self._batch_size = batch_size
        self._shuffle = shuffle
        self._path = path
        self._eval = is_eval

        if not self._eval:
            self._transforms = transforms.Compose(
                [
                    transforms.RandomResizedCrop(self._input_size),
                    transforms.RandomHorizontalFlip(),
                    transforms.ToTensor(),
                    transforms.Normalize([0.485, 0.456, 0.406],
                                         [0.229, 0.224, 0.225]),
                ]
            )

        else:
            # These are tuned for ImageNet evaluation
            self._transforms = transforms.Compose(
                [
                    transforms.Resize(256),
                    transforms.CenterCrop(self._input_size),
                    transforms.ToTensor(),
                    transforms.Normalize([0.485, 0.456, 0.406],
                                         [0.229, 0.224, 0.225]),
                ]
            )

        self._dataset = datasets.ImageFolder(self._path, self._transforms)

    def __len__(self):
        return len(self._dataset)

    # @property
    # def data_iterator(self):
    #   if self.placement == DeviceType.AllGpu:
    #     self.train_sampler = t.utils.data.distributed.DistributedSampler(
    #     self._dataset)
    #   else:
    #     self.train_sampler = None
    #   return t.utils.data.DataLoader(self._dataset,
    #   batch_size=self._batch_size,
    #                                  shuffle=(self.train_sampler == None),
    #                                  num_workers=4,
    #                                  sampler=self.train_sampler)
    @property
    def dataset(self):
        return self._dataset

    @property
    def data_iterator(self):
        return None

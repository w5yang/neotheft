import argparse
from typing import Dict, Any, List, Tuple
import torch
from torch.nn import Module
from torch.optim.optimizer import Optimizer
import os
from tqdm import tqdm
import pickle
import datasets

from knockoff.victim.blackbox import Blackbox
from models import zoo
from torch import Tensor
from torch import device as Device

import os
import numpy as np
import imgaug as ia
import imgaug.augmenters as iaa
from PIL import Image
from torchvision import transforms


def parser_dealer(option: Dict[str, bool]) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(description='Train a model')
    # Required arguments
    if option['transfer']:
        parser.add_argument('policy', metavar='PI', type=str, help='Policy to use while training',
                            choices=['random', 'adaptive'])
        parser.add_argument('--budget', metavar='N', type=int, help='Size of transfer set to construct',
                            required=True)
        parser.add_argument('--out_dir', metavar='PATH', type=str,
                            help='Destination directory to store transfer set', required=True)
        parser.add_argument('--queryset', metavar='TYPE', type=str, help='Adversary\'s dataset (P_A(X))', required=True)

    if option['active']:
        parser.add_argument('strategy', metavar='S', type=str, help='Active Sample Strategy',
                            choices=['kcenter', 'random', 'dfal'])
        parser.add_argument('--metric', metavar="M", type=str, help='K-Center method distance metric',
                            choices=['euclidean', 'manhattan', 'l1', 'l2'], default='euclidean')
        parser.add_argument('--initial-size', metavar='N', type=int, help='Active Learning Initial Sample Size',
                            default=100)
        parser.add_argument('--budget-per-iter', metavar='N', type=int, help='budget for every iteration',
                            default=100)
        parser.add_argument('--iterations', metavar='N', type=int, help='iteration times',
                            default=10)
    if option['sampling']:
        parser.add_argument('sampleset', metavar='DS_NAME', type=str,
                            help='Name of sample dataset in active learning selecting algorithms')
        parser.add_argument('--selected-path', metavar='SE', type=str,
                            help='remove selected samples from sample set', required=False)
    if option['synthetic']:
        parser.add_argument('synthetic_method', metavar='SM', type=str, help='Synthetic Method',
                            choices=['fgsm', 'ifgsm', 'mifgsm'])
        parser.add_argument('eps', metavar='E', type=float, help='Synthetic maximum epsilon')
        parser.add_argument('targeted_method', metavar='T', type=str, help='Target methods',
                            choices=['non-targeted', 'targeted-random', 'targeted-topk'])
    if option['black_box']:
        parser.add_argument('victim_model_dir', metavar='VIC_DIR', type=str,
                            help='Path to victim model. Should contain files "model_best.pth.tar" and "params.json"')
        parser.add_argument('--argmaxed', action='store_true', help='Only consider argmax labels', default=False)
        parser.add_argument('--topk', metavar='TK', type=int, help='iteration times',
                            default=0)
    if option['train']:
        parser.add_argument('model_dir', metavar='SUR_DIR', type=str, help='Surrogate Model Destination directory')
        parser.add_argument('model_arch', metavar='MODEL_ARCH', type=str, help='Model name')
        parser.add_argument('testdataset', metavar='DS_NAME', type=str, help='Name of test')
        # Optional arguments
        parser.add_argument('-e', '--epochs', type=int, default=100, metavar='N',
                            help='number of epochs to train (default: 100)')
        parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                            help='learning rate (default: 0.01)')
        parser.add_argument('--momentum', type=float, default=0.5, metavar='M',
                            help='SGD momentum (default: 0.5)')
        parser.add_argument('--log-interval', type=int, default=50, metavar='N',
                            help='how many batches to wait before logging training status')
        parser.add_argument('--resume', default=None, type=str, metavar='PATH',
                            help='path to latest checkpoint (default: none)')
        parser.add_argument('--lr-step', type=int, default=60, metavar='N',
                            help='Step sizes for LR')
        parser.add_argument('--lr-gamma', type=float, default=0.1, metavar='N',
                            help='LR Decay Rate')
        parser.add_argument('--pretrained', type=str, help='Use pretrained network', default=None)
        parser.add_argument('--weighted-loss', action='store_true', help='Use a weighted loss', default=False)
        parser.add_argument('--optimizer_choice', type=str, help='Optimizer', default='sgdm',
                            choices=('sgd', 'sgdm', 'adam', 'adagrad'))
    # apply to all circumstances
    parser.add_argument('-b', '--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('-d', '--device_id', metavar='D', type=int, help='Device id. -1 for CPU.', default=0)
    parser.add_argument('-w', '--num_workers', metavar='N', type=int, help='# Worker threads to load data',
                        default=10)
    args = parser.parse_args()
    params = vars(args)
    device = device_dealer(**params)
    params['device'] = device
    if option['black_box']:
        blackbox_dir = params['victim_model_dir']
        params['blackbox'] = Blackbox.from_modeldir(blackbox_dir, device)
    if option['active']:
        pass
    if option['sampling']:
        sample_set_name = params['sampleset']
        assert sample_set_name in datasets.__dict__.keys()
        modelfamily = datasets.dataset_to_modelfamily[sample_set_name]
        transform = datasets.modelfamily_to_transforms[modelfamily]['test']
        dataset = datasets.__dict__[sample_set_name](train=True, transform=transform)
        params['queryset'] = dataset
        params['selected'] = set()
        if params['selected_path'] is not None:
            total = set([i for i in range(len(dataset))])
            path = params['selected_path']
            with open(path, 'rb') as fp:
                selected = pickle.load(fp)
            params['selected'] = selected
    if option['train']:
        testset_name = params['testdataset']
        assert testset_name in datasets.__dict__.keys()
        modelfamily = datasets.dataset_to_modelfamily[testset_name]
        transform = datasets.modelfamily_to_transforms[modelfamily]['test']
        testset = datasets.__dict__[testset_name](train=False, transform=transform)
        params['testset'] = testset
        num_classes = len(testset.classes)
        pretrained_path = params['pretrained']
        model_arch = params['model_arch']
        sample = testset[0][0]

        model = zoo.get_net(model_arch, modelfamily, pretrained_path, num_classes=num_classes, channel=sample.shape[0])
        params['surrogate'] = model.to(device)
    return params


def device_dealer(**params) -> torch.device:
    if params['device_id'] >= 0:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(params['device_id'])
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')
    return device


def query(
        blackbox: Blackbox,
        training_samples: List[Tensor],
        budget: int,
        argmax: bool = False,
        batch_size: int = 1024,
        device: Device = Device('cpu'),
        topk: int = 0,
) -> List:
    results = []
    with tqdm(total=budget) as pbar:
        for t, B in enumerate(range(0, len(training_samples), batch_size)):
            x_t = torch.stack([training_samples[i] for i in range(B, min(B + batch_size, budget))]).to(device)
            y_t = blackbox(x_t)
            if argmax:
                y_t = y_t.argmax(1)
            elif topk != 0:
                v, i = y_t.topk(topk, 1)
                y_t = torch.zeros_like(y_t).scatter(1, i, v)
            # unpack
            for i in range(x_t.size(0)):
                results.append((x_t[i].cpu(), y_t[i].cpu()))
            pbar.update(x_t.size(0))
    return results


def load_transferset(path: str, topk: int = 0, argmax: bool = False) -> (List, int):
    assert os.path.exists(path)
    with open(path, 'rb') as rf:
        samples = pickle.load(rf)
    if argmax:
        results = [(item[0], int(item[1].argmax())) for item in samples]
    elif topk != 0:
        results = []
        for x, y in samples:
            values, indices = y.topk(topk)
            z = torch.zeros_like(y).scatter(0, indices, values)
            results.append((x, z))
    else:
        results = samples
    num_classes = samples[0][1].size(0)
    return results, num_classes


def save_selection_state(data: List[Tuple[Tensor, Tensor]], selection: dict, state_dir: str) -> None:
    if os.path.exists(state_dir):
        assert os.path.isdir(state_dir)
    else:
        os.mkdir(state_dir)
    transfer_path = os.path.join(state_dir, 'transferset.pickle')
    if os.path.exists(transfer_path):
        print('Override previous transferset => {}'.format(transfer_path))
    with open(transfer_path, 'wb') as tfp:
        pickle.dump(data, tfp)
    print("=> selected {} samples written to {}".format(len(data), transfer_path))
    selection_path = os.path.join(state_dir, 'selection.pickle')
    if os.path.exists(selection_path):
        print('Override previous selected index => {}'.format(selection_path))
    with open(selection_path, 'wb') as sfp:
        pickle.dump(selection, sfp)
    print("=> selected {} sample indices written to {}".format(len(selection), selection_path))


def load_state(state_dir: str, selection_suffix: str = None) -> (set, List):
    transfer_path = os.path.join(state_dir, 'transferset.{}pickle'.format(selection_suffix))
    selection_path = os.path.join(state_dir, 'selection.{}pickle'.format(selection_suffix))
    if not os.path.exists(transfer_path) or not os.path.exists(selection_path):
        print("State not exists, returning None")
        return set(), []
    with open(selection_path, 'rb') as sf:
        selection = pickle.load(sf)
        assert isinstance(selection, set)
        print("=> load selected {} sample indices from {}".format(len(selection), selection_path))
    with open(transfer_path, 'rb') as tf:
        transfer = pickle.load(tf)
        assert isinstance(transfer, List)
        print("=> load selected {} samples from {}".format(len(transfer), transfer_path))
    return selection, transfer


seq = iaa.Sequential([
    # iaa.Fliplr(0.5), # horizontal flips
    iaa.Crop(percent=(0, 0.1)),  # random crops
    # Small gaussian blur with random sigma between 0 and 0.5.
    # But we only blur about 50% of all images.
    iaa.Sometimes(
        0.5,
        iaa.GaussianBlur(sigma=(0, 0.1))
    ),
    # Strengthen or weaken the contrast in each image.
    iaa.LinearContrast((0.75, 1.5)),
    # Add gaussian noise.
    # For 50% of all images, we sample the noise once per pixel.
    # For the other 50% of all images, we sample the noise per pixel AND
    # channel. This can change the color (not only brightness) of the
    # pixels.
    iaa.AdditiveGaussianNoise(loc=0, scale=(0.0, 0.05 * 255), per_channel=0.5),
    # Make some images brighter and some darker.
    # In 20% of all cases, we sample the multiplier once per channel,
    # which can end up changing the color of the images.
    # iaa.Multiply((0.8, 1.2), per_channel=0.2),
    # Apply affine transformations to each image.
    # Scale/zoom them, translate/move them, rotate them and shear them.
    iaa.Affine(
        scale={"x": (0.8, 1.2), "y": (0.8, 1.2)},
        translate_percent={"x": (-0.2, 0.2), "y": (-0.2, 0.2)},
        rotate=(-20, 20),
        shear=(-8, 8)
    )
], random_order=True)  # apply augmenters in random order


def save_npimg(array: np.ndarray, path: str) -> None:
    """ Save numpy array to image file.

    :param array: img array
    :param path: path including corresponding extension
    :return: None
    """
    img = Image.fromarray(array.squeeze())
    img.save(path)


def augment(img, expand_factor: int) -> np.ndarray:
    """Expand input image to a quantity of expand_factor

    :param img: numpy.ndarray, already converted to 'uint8' shape=(H, W, C);
       torch.Tensor, unconverted, may be 'float32' shape=(C, H, W), shape=(B, C, H, W);
       PIL.Image
    :param expand_factor: Quantity of generated images.
    :return: result array, 'uint8' shape=(expand_factor, H, W, C)
    """
    if isinstance(img, Tensor):
        img = tensor_to_np(img)
    elif isinstance(img, np.ndarray):
        pass
    elif isinstance(img, Image.Image):
        img = np.asarray(img, dtype="uint8")
    else:
        raise ValueError
    img_batch = np.expand_dims(img, 0).repeat(expand_factor, 0)
    images_aug = seq(images=img_batch)  # (200,28,28,1)
    # images_aug_trans = np.expand_dims(images_aug,1)
    print("images_aug: ", images_aug.shape)
    return images_aug


def tensor_to_np(tensor: Tensor) -> np.ndarray:
    img = tensor.mul(255).byte()
    img = img.cpu()
    if len(img.shape) == 4:
        img.squeeze_(0)
    elif len(img.shape) == 3:
        pass
    else:
        raise ValueError
    img = img.numpy().transpose((1, 2, 0))
    return img


def load_img_dir(img_dir: str, transform=None) -> List[torch.tensor]:
    imgs = []
    if transform is None:
        transform = transforms.ToTensor()
    for file in os.listdir(img_dir):
        img = Image.open(os.path.join(img_dir, file))
        imgs.append(img)
    return [transform(img) for img in imgs]


# This function unpack the image tensor out of dataset-like List
unpack = lambda x: [item[0] for item in x]

if __name__ == '__main__':
    # test
    parser_dealer(option={
        'transfer': False,
        'active': True,
        'synthetic': False,
        'black_box': True,
        'train': True
    })

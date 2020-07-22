import torch
import numpy as np
from torch.utils.data import DataLoader
from torch.autograd import Variable
from tqdm import tqdm

from typing import List, Tuple, Dict
from torch.nn import Module
from torch import Tensor

from neotheft.utils.synthetic_sample_crafter import SyntheticSampleCrafter, FGSM, IFGSM, MIFGSM
from knockoff.victim.blackbox import Blackbox


def transferability(blackbox: Blackbox,
                    surrogate: Module,
                    data: List[Tuple[Tensor, int]],
                    method: str = "ifgsm",
                    targeted: bool = False,
                    targets_dict: Dict = None,
                    option: Dict = None,
                    batch_size: int = 64,
                    num_workers: int = 8) -> float:
    if option is None:
        option = {
            "esp": 64,
            "min_pixel": 0.0,
            "max_pixel": 1.0,
            "is_cuda": True
        }
    cuda = lambda x: x.cuda() if option["is_cuda"] else lambda x: x.cpu()
    surrogate = cuda(surrogate)
    blackbox = cuda(blackbox)
    if targets_dict is None:
        targets_dict = dict()
        sample = data[0][0]
        surrogate.eval()
        sample_result = surrogate(cuda(sample.unsqueeze(0)))
        num_classes = sample_result.shape[1]
        for i in range(num_classes):
            targets_dict[i] = i
    assert method in ("ifgsm", "mifgsm")
    loader = DataLoader(data, batch_size=batch_size, shuffle=True, num_workers=num_workers)
    if method == "ifgsm":
        crafter = IFGSM(targeted_attack=targeted, **option)
    elif method == "mifgsm":
        crafter = MIFGSM(targeted_attack=targeted, **option)
    else:
        raise NotImplementedError

    num_steps = 40
    initial_alpha = option['eps'] / num_steps

    total = len(data)
    agreement = 0
    transfer = 0

    for inputs, _ in tqdm(loader, desc='Batch'):
        # surrogate output
        outputs_sur = surrogate(Variable(cuda(inputs), requires_grad=False))
        labels_sur = torch.max(outputs_sur, 1)[1]
        # blackbox output
        outputs_bb = blackbox(cuda(inputs))
        labels_bb = torch.max(outputs_bb, 1)[1]
        # target directions
        targets = torch.tensor([targets_dict[int(i)] for i in labels_sur])
        x_adv = crafter(surrogate, inputs, targets, initial_alpha, num_steps)
        adv_output_sur = surrogate(Variable(cuda(x_adv), requires_grad=False))
        adv_labels_sur = adv_output_sur.max(1)[1]
        adv_output_bb = blackbox(cuda(x_adv))
        adv_labels_bb = adv_output_bb.max(1)[1]
        agreement += torch.sum(labels_bb == labels_sur)
        transfer += torch.sum(adv_labels_bb == targets) if targeted else torch.sum(adv_labels_bb != targeted)
    agreement /= total
    transfer /= total
    print("Agreement: {}".format(agreement))
    print("Transferability: {}".format(transfer))
    return transfer


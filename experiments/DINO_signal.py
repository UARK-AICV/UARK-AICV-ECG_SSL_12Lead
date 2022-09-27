import os
import numpy as np
from tqdm import tqdm
import torch
from torch.optim.lr_scheduler import CosineAnnealingLR
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.nn as nn
import torch.distributed as dist
import math 

import sys 
import warnings
current_path = os.getcwd()
sys.path.append(current_path)

from models.signal_model import signal_model
from utils.DINO_dataloader import ECG_dataset_DINO_signal
from utils.tools import weights_init_xavier, cancel_gradients_last_layer, set_requires_grad, cosine_scheduler

ctx = "cuda:0" if torch.cuda.is_available() else 'cpu'
rank, gpu, world_size = 0, 0, 1
os.environ['MASTER_ADDR'] = '127.0.0.1'
os.environ['MASTER_PORT'] = '29500'
dist.init_process_group(
        backend="nccl",
        init_method="env://",
        world_size=world_size,
        rank=rank,
    )
torch.cuda.set_device(0)
dist.barrier()

class MultiCropWrapper(nn.Module):
    """
    Perform forward pass separately on each resolution input.
    The inputs corresponding to a single resolution are clubbed and single
    forward is run on the same resolution inputs. Hence we do several
    forward passes = number of different resolutions used. We then
    concatenate all the output features and run the head forward on these
    concatenated features.
    """
    def __init__(self, backbone, head):
        super(MultiCropWrapper, self).__init__()
        # disable layers dedicated to ImageNet labels classification
        backbone.fc, backbone.head = nn.Identity(), nn.Identity()
        self.backbone = backbone
        self.head = head

    def forward(self, x):
        # convert to list
        if not isinstance(x, list):
            x = [x]
        idx_crops = torch.cumsum(torch.unique_consecutive(
            torch.tensor([inp.shape[-1] for inp in x]),
            return_counts=True,
        )[1], 0)
        start_idx, output = 0, torch.empty(0).to(x[0].device)
        for end_idx in idx_crops:
            _out = self.backbone(torch.cat(x[start_idx: end_idx]))
            # The output is a tuple with XCiT model. See:
            # https://github.com/facebookresearch/xcit/blob/master/xcit.py#L404-L405
            if isinstance(_out, tuple):
                _out = _out[0]
            # accumulate outputs
            output = torch.cat((output, _out))
            start_idx = end_idx
        # Run the head forward on the concatenated features.
        return self.head(output)

def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    # Cut & paste from PyTorch official master until it's in a few official releases - RW
    # Method based on https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf
    def norm_cdf(x):
        # Computes standard normal cumulative distribution function
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    if (mean < a - 2 * std) or (mean > b + 2 * std):
        warnings.warn("mean is more than 2 std from [a, b] in nn.init.trunc_normal_. "
                      "The distribution of values may be incorrect.",
                      stacklevel=2)

    with torch.no_grad():
        # Values are generated by using a truncated uniform distribution and
        # then using the inverse CDF for the normal distribution.
        # Get upper and lower cdf values
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)

        # Uniformly fill tensor with values from [l, u], then translate to
        # [2l-1, 2u-1].
        tensor.uniform_(2 * l - 1, 2 * u - 1)

        # Use inverse cdf transform for normal distribution to get truncated
        # standard normal
        tensor.erfinv_()

        # Transform to proper mean, std
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)

        # Clamp to ensure it's in the proper range
        tensor.clamp_(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    # # type: (Tensor, float, float, float, float) -> Tensor
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)

class DINOHead(nn.Module):
    def __init__(self, in_dim, out_dim, use_bn=False, norm_last_layer=True, nlayers=3, hidden_dim=512, bottleneck_dim=128):
        super().__init__()
        nlayers = max(nlayers, 1)
        if nlayers == 1:
            self.mlp = nn.Linear(in_dim, bottleneck_dim)
        else:
            layers = [nn.Linear(in_dim, hidden_dim)]
            if use_bn:
                layers.append(nn.BatchNorm1d(hidden_dim))
            layers.append(nn.GELU())
            for _ in range(nlayers - 2):
                layers.append(nn.Linear(hidden_dim, hidden_dim))
                if use_bn:
                    layers.append(nn.BatchNorm1d(hidden_dim))
                layers.append(nn.GELU())
            layers.append(nn.Linear(hidden_dim, bottleneck_dim))
            self.mlp = nn.Sequential(*layers)
        self.apply(self._init_weights)
        self.last_layer = nn.utils.weight_norm(nn.Linear(bottleneck_dim, out_dim, bias=False))
        self.last_layer.weight_g.data.fill_(1)
        if norm_last_layer:
            self.last_layer.weight_g.requires_grad = False

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)

    def forward(self, x):
        x = self.mlp(x)
        x = nn.functional.normalize(x, dim=-1, p=2)
        x = self.last_layer(x)
        return x

class DINOLoss(nn.Module):
    def __init__(self, out_dim, ncrops, warmup_teacher_temp, teacher_temp,
                 warmup_teacher_temp_epochs, nepochs, student_temp=0.1,
                 center_momentum=0.9):
        super().__init__()
        self.student_temp = student_temp
        self.center_momentum = center_momentum
        self.ncrops = ncrops
        self.register_buffer("center", torch.zeros(1, out_dim))
        # we apply a warm up for the teacher temperature because
        # a too high temperature makes the training instable at the beginning
        self.teacher_temp_schedule = np.concatenate((
            np.linspace(warmup_teacher_temp,
                        teacher_temp, warmup_teacher_temp_epochs),
            np.ones(nepochs - warmup_teacher_temp_epochs) * teacher_temp
        ))

    def forward(self, student_output, teacher_output, epoch):
        """
        Cross-entropy between softmax outputs of the teacher and student networks.
        """
        student_out = student_output / self.student_temp
        student_out = student_out.chunk(self.ncrops)

        # teacher centering and sharpening
        temp = self.teacher_temp_schedule[epoch]
        teacher_out = F.softmax((teacher_output - self.center) / temp, dim=-1)
        teacher_out = teacher_out.detach().chunk(2)

        total_loss = 0
        n_loss_terms = 0
        for iq, q in enumerate(teacher_out):
            for v in range(len(student_out)):
                if v == iq:
                    # we skip cases where student and teacher operate on the same view
                    continue
                loss = torch.sum(-q * F.log_softmax(student_out[v], dim=-1), dim=-1)
                total_loss += loss.mean()
                n_loss_terms += 1
        total_loss /= n_loss_terms
        self.update_center(teacher_output)
        return total_loss

    @torch.no_grad()
    def update_center(self, teacher_output):
        """
        Update center used for teacher output.
        """
        batch_center = torch.sum(teacher_output, dim=0, keepdim=True)
        dist.all_reduce(batch_center)
        batch_center = batch_center / (len(teacher_output) * dist.get_world_size())
        batch_center = batch_center / len(teacher_output)

        # ema update
        self.center = self.center * self.center_momentum + batch_center * (1 - self.center_momentum)

def is_dist_avail_and_initialized():
    if not dist.is_available():
        return False
    if not dist.is_initialized():
        return False
    return True

def get_world_size():
    if not is_dist_avail_and_initialized():
        return 1
    return dist.get_world_size()

def run():
    root_folder = './data_folder'
    data_folder = os.path.join(root_folder,'data_summary_without_preprocessing')
    # equivalent_classes = [['CRBBB', 'RBBB'], ['PAC', 'SVPB'], ['PVC', 'VPB']]
    equivalent_classes = [['713427006', '59118001'], ['284470004', '63593006'], ['427172004', '17338001']]

    no_channels = 12 
    signal_size = 250
    train_stride = signal_size
    train_chunk_length = 0

    transforms = ["TimeOut_difflead","GaussianNoise"]

    batch_size = 196
    learning_rate = 5e-4
    min_learing_rate = 1e-6
    no_epoches = 301

    get_mean = np.load(os.path.join(data_folder,"mean.npy"))
    get_std = np.load(os.path.join(data_folder,"std.npy"))

    t_params = {"gaussian_scale":[0.005,0.025], "global_crop_scale": [0.5, 1.0], "local_crop_scale": [0.1, 0.5],
                "output_size": 250, "warps": 3, "radius": 10, "shift_range":[0.2,0.5],
                "epsilon": 10, "magnitude_range": [0.5, 2], "downsample_ratio": 0.2, "to_crop_ratio_range": [0.2, 0.4],
                "bw_cmax":0.1, "em_cmax":0.5, "pl_cmax":0.2, "bs_cmax":1, "stats_mean":get_mean,"stats_std":get_std}

    train_dataset = ECG_dataset_DINO_signal(summary_folder=data_folder, signal_size=signal_size, stride=train_stride,
                            chunk_length=train_chunk_length,transforms=transforms,t_params=t_params,
                            equivalent_classes=equivalent_classes, sample_items_per_record=1, random_crop=True)
    train_dataloader = DataLoader(train_dataset, shuffle=True, num_workers=4,batch_size=batch_size,drop_last=True)

    no_classes = 24
    student = signal_model(no_classes)
    teacher = signal_model(no_classes)
    embed_dim = student.fc[0].weight.shape[1]

    out_dim = 65536
    local_crops_number = 8
    warmup_epochs = 10 # recommended 30
    warmup_teacher_temp = 0.04
    teacher_temp = 0.04
    warmup_teacher_temp_epochs = 30
    weight_decay = 0.04
    weight_decay_end = 0.4
    momentum_teacher = 0.996

    student = MultiCropWrapper(student, DINOHead(
        embed_dim,
        out_dim,  # out_dim
        use_bn=False,
        norm_last_layer=True,
    ))
    teacher = MultiCropWrapper(
        teacher,
        DINOHead(embed_dim, out_dim, False),
    )
    student.apply(weights_init_xavier)
    student.to(ctx)
    teacher.to(ctx)
    teacher.load_state_dict(student.state_dict())
    set_requires_grad(teacher,False)

    # ============ preparing loss ... ============
    dino_loss = DINOLoss(
        out_dim,
        local_crops_number + 2,  # total number of crops = 2 global crops + local_crops_number
        warmup_teacher_temp,
        teacher_temp,
        warmup_teacher_temp_epochs,
        no_epoches,
    ).to(ctx)
    
    # optimizer = LARS(student.parameters(),lr=0.1,weight_decay=0.0048)
    optimizer = torch.optim.Adam(student.parameters(),lr=0) # scheduler controls the learning rate
    scheduler_steplr = CosineAnnealingLR(optimizer, no_epoches, eta_min=1e-4, last_epoch=-1)
    
    # # ============ init schedulers ... ============
    lr_schedule = cosine_scheduler(
        learning_rate* (batch_size * get_world_size()) / 256.,  # linear scaling rule
        min_learing_rate,
        no_epoches, len(train_dataloader),
        warmup_epochs=warmup_epochs,
    )
    wd_schedule = cosine_scheduler(
        weight_decay,
        weight_decay_end,
        no_epoches, len(train_dataloader),
    )
    # momentum parameter is increased to 1. during training with a cosine schedule
    # momentum_schedule = cosine_scheduler(momentum_teacher, 1,no_epoches, len(train_dataloader))


    optimizer.zero_grad()
    optimizer.step()
    student.train()
    lowest_train_loss = 10
    for epoch in range(1,no_epoches+1):
        print('===================Epoch [{}/{}]'.format(epoch,no_epoches))
        print('Current lr: ',optimizer.param_groups[0]['lr'],', wd:',optimizer.param_groups[0]['weight_decay'])
        
        # scheduler_steplr.step()
        train_loss = 0
        for batch_idx, sample in enumerate(tqdm(train_dataloader)):
            it = len(train_dataloader) * epoch + batch_idx  # global training iteration
            for i, param_group in enumerate(optimizer.param_groups):
                param_group["lr"] = lr_schedule[it]
                if i == 0:  # only the first group is regularized
                    param_group["weight_decay"] = wd_schedule[it]
                
            data = sample['crops']
            gpu_data = [im.to(ctx).float() for im in data]

            teacher_output = teacher(gpu_data[:2])  # only the 2 global views pass through the teacher
            student_output = student(gpu_data)  # all the views including the 2 global views

            loss = dino_loss(student_output, teacher_output, epoch)
            train_loss += loss.item()

            optimizer.zero_grad()
            loss.backward()
            cancel_gradients_last_layer(epoch, student,1)
            optimizer.step()

            with torch.no_grad():
                # m = momentum_schedule[it]  # momentum parameter
                m = 0.996
                for param_q, param_k in zip(student.parameters(), teacher.parameters()):
                    param_k.data.mul_(m).add_((1 - m) * param_q.detach().data)

        whole_train_loss = train_loss / (batch_idx + 1)
        print(f'Train Loss: {whole_train_loss}')
        if whole_train_loss < lowest_train_loss:
            lowest_train_loss = whole_train_loss
            torch.save(student.backbone.state_dict(), f'./checkpoints/DINO_signal_student.pth')
            torch.save(teacher.backbone.state_dict(), f'./checkpoints/DINO_signal_teacher.pth')


if __name__ == "__main__":
    run()
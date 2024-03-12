import sys
import os

sys.path.append(os.getcwd())

import torch
from torch import nn
import global_v as glv
from datasets import load_datasets
from models.generators import *
from models.discriminators import *
from network_parser import Parse
import torchvision
from torchvision.utils import save_image
import argparse
import logging
from tqdm import tqdm, trange
import math
from spikingjelly.datasets import play_frame
from einops import repeat
import tonic
from torchvision import transforms
from tonic import DiskCachedDataset
import torch.nn.functional as F

DATA_DIR = "../dataset"


def dvs_channel_check_expend(x):
    """
    检查是否存在DVS数据缺失, N-Car中有的数据会缺少一个通道
    :param x: 输入的tensor
    :return: 补全之后的数据
    """
    if x.shape[1] == 1:
        return repeat(x, 'b c w h -> b (r c) w h', r=2)
    else:
        return x


def load_nmnist_denoise(batch_size, step, size):

    sensor_size = tonic.datasets.NMNIST.sensor_size
    filter_time = 10000

    train_transform = transforms.Compose([
        tonic.transforms.Denoise(filter_time=filter_time),
        # tonic.transforms.DropEvent(p=0.1),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])
    test_transform = transforms.Compose([
        tonic.transforms.Denoise(filter_time=filter_time),
        tonic.transforms.ToFrame(sensor_size=sensor_size, n_time_bins=step),
    ])

    train_dataset = tonic.datasets.NMNIST(os.path.join(DATA_DIR,
                                                       'DVS/N-MNIST'),
                                          transform=train_transform,
                                          train=True)
    test_dataset = tonic.datasets.NMNIST(os.path.join(DATA_DIR, 'DVS/N-MNIST'),
                                         transform=test_transform,
                                         train=False)

    train_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(
            x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
        # transforms.RandomCrop(size, padding=size // 12),
        # transforms.RandomHorizontalFlip(),
        # transforms.RandomRotation(15)
    ])
    test_transform = transforms.Compose([
        lambda x: torch.tensor(x, dtype=torch.float),
        lambda x: F.interpolate(
            x, size=[size, size], mode='bilinear', align_corners=True),
        lambda x: dvs_channel_check_expend(x),
    ])

    train_dataset = DiskCachedDataset(
        train_dataset,
        cache_path=os.path.join(
            DATA_DIR, 'DVS/N-MNIST/train_cache_{}_denoise'.format(step)),
        transform=train_transform,
        num_copies=3)
    test_dataset = DiskCachedDataset(
        test_dataset,
        cache_path=os.path.join(
            DATA_DIR, 'DVS/N-MNIST/test_cache_{}_denoise'.format(step)),
        transform=test_transform,
        num_copies=3)

    train_loader = torch.utils.data.DataLoader(
        train_dataset,
        batch_size=batch_size,
        pin_memory=True,
        drop_last=True,
        num_workers=8,
        shuffle=True,
    )

    test_loader = torch.utils.data.DataLoader(
        test_dataset,
        batch_size=batch_size,
        pin_memory=True,
        drop_last=False,
        num_workers=2,
        shuffle=False,
    )

    return train_loader, test_loader


def dvs_decode(x: torch.Tensor, method='none'):
    """
    x.shape = (T,...)
    """
    if method == 'none':
        return x
    elif method == 'mean':
        return torch.mean(x, 0)
    elif method == 'last':
        return x[-1]


def reset_net(net: nn.Module):
    for m in net.modules():
        if hasattr(m, 'n_reset'):
            m.n_reset()


def update_D(X, Z, net_D, net_G, trainer_D, img_size, channels):
    # X.shape = (batch_size, 784)
    batch_size = X.shape[0]
    one = torch.tensor(1.0)
    trainer_D.zero_grad()
    real_Y = net_D(X, is_imgs=True)  # real_Y.shape = (n_steps, batch_size, 1)
    # print(real_Y.shape)
    if not glv.network_config['is_mem']:
        real_Y = torch.sum(real_Y, dim=0) / glv.network_config[
            'n_steps']  # real_Y.shape = (batch_size,1)
    real_Y = real_Y.mean()
    real_Y.backward(one)
    reset_net(net_D)
    # print(real_Y.shape)
    fake_X = net_G(Z)  # fake_X.shape = (n_steps, batch_size, 784)
    n_step = fake_X.shape[0]
    if glv.network_config['net_D_direct_input']:
        fake_X = fake_X.reshape(
            (n_step, batch_size, channels, img_size, img_size))
    else:
        fake_X = fake_X.reshape((n_step, batch_size, img_size**2))
    fake_Y = net_D(fake_X.detach())  # fake_Y.shape = (n_steps, batch_size, 1)
    if not glv.network_config['is_mem']:
        fake_Y = torch.sum(fake_Y, dim=0) / glv.network_config[
            'n_steps']  # fake_Y.shape = (batch_size,1)
    fake_Y = fake_Y.mean()
    total_loss = -fake_Y
    # fake_Y.backward(mone)
    total_loss.backward()
    trainer_D.step()
    # print(fake_Y.data, real_Y.data)
    return fake_Y.data, real_Y.data


def update_G(Z, net_D, net_G, trainer_G, img_size, channels):
    batch_size = Z.shape[0]
    one = torch.tensor(1.0)
    trainer_G.zero_grad()
    fake_X = net_G(Z)  # fake_X.shape = (n_steps, batch_size, 784)
    n_step = fake_X.shape[0]
    if glv.network_config['net_D_direct_input']:
        fake_X = fake_X.reshape(
            (n_step, batch_size, channels, img_size, img_size))
    else:
        fake_X = fake_X.reshape((n_step, batch_size, img_size**2))
    fake_Y = net_D(fake_X)  # shape = (n_steps, batch_size, 1)
    if not glv.network_config['is_mem']:
        fake_Y = torch.sum(
            fake_Y,
            dim=0) / glv.network_config['n_steps']  # shape = (batch_size, 1)\
    fake_Y = fake_Y.mean()
    fake_Y.backward(one)
    trainer_G.step()
    # print(f'Y_before:{fake_Y.detach()}')
    '''with torch.no_grad():
        Y_after = net_D(fake_X.detach())
        if not glv.network_config['is_mem']:
            Y_after = torch.sum(Y_after, dim=0) / glv.network_config['n_steps']  # fake_Y.shape = (batch_size,1)
        Y_after = Y_after.mean()
        print(f'Y_after:{Y_after}')
        print(" ")'''
    return fake_Y.data


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    '''parser.add_argument("--name", required=True, dest='name', type=str)
    parser.add_argument("--exp_index",
                        required=True,
                        dest='exp_index',
                        type=str)'''
    parser.add_argument("--config", required=True, dest='config', type=str)
    args = parser.parse_args()

    config = args.config
    params = Parse(config)
    glv.init(params['Network'])

    data_path = glv.network_config['data_path']
    device = glv.network_config['device']
    name = glv.network_config['name']
    dataset_name = glv.network_config['dataset']
    latent_dim = glv.network_config['latent_dim']
    lr_D = glv.network_config['lr_D']
    lr_G = glv.network_config['lr_G']
    # torch.autograd.set_detect_anomaly(True)

    os.makedirs(f'./exp_results/checkpoints/{name}', exist_ok=True)
    os.makedirs(f'./exp_results/images/{name}', exist_ok=True)
    logging.basicConfig(filename=f'./exp_results/logs/{name}.log',
                        level=logging.INFO)

    # load dataset
    print("loading dataset")
    if dataset_name == 'MNIST':
        trainloader, _ = load_datasets.load_mnist(
            data_path, is_normlized=glv.network_config['is_data_normlized'])
    elif dataset_name == "CelebA":
        trainloader, _ = load_datasets.load_CelebA(
            data_path, is_normlized=glv.network_config['is_data_normlized'])
    elif dataset_name == "dvs_cifar10_64":
        trainloader, _, _, _ = get_dvsc10_data(
            batch_size=glv.network_config['batch_size'],
            step=glv.network_config['n_steps'],
            root='../dataset',
            size=64)
    elif dataset_name == "dvs_mnist_28":
        trainloader, _, _, _ = get_nmnist_data(
            batch_size=glv.network_config['batch_size'],
            step=glv.network_config['n_steps'],
            root='../dataset',
            size=28)
    elif dataset_name == "dvs_mnist_28_denoise":
        trainloader, _ = load_nmnist_denoise(
            batch_size=glv.network_config['batch_size'],
            step=glv.network_config['n_steps'],
            size=28)

    # load model
    net_G, net_D = None, None
    if dataset_name == "MNIST":
        net_G = Generator_MP_Scoring_Mnist(input_dim=latent_dim)
        net_D = Discriminator_EM_MNIST()
    elif dataset_name == "CelebA":
        net_G = Generator_MP_Scoring_CelebA(input_dim=latent_dim)
        net_D = Discriminator_EM_CelebA()
    elif dataset_name == "dvs_cifar10_64":
        net_G = Generator_MP_Scoring_DVS_64(input_dim=latent_dim,
                                            is_split=True)
        net_D = Discriminator_EM_DVS_64()
    elif dataset_name == 'dvs_mnist_28' or 'dvs_mnist_28_denoise':
        net_G = Generator_MP_Scoring_DVS_28(input_dim=latent_dim,
                                            is_split=True)
        net_D = Discriminator_EM_DVS_28()

    # set optimizer
    optimizer_G = torch.optim.RMSprop(net_G.parameters(), lr=lr_G)
    optimizer_D = torch.optim.RMSprop(net_D.parameters(), lr=lr_D)

    # to device
    net_G = net_G.to(device)
    net_D = net_D.to(device)

    init_epoch = 0
    if glv.network_config['from_checkpoint']:
        print("loading checkpoint")
        checkpoint = torch.load(glv.network_config['checkpoint_path'])
        init_epoch = checkpoint['epoch']
        net_D.load_state_dict(checkpoint['model_state_dict_D'])
        net_G.load_state_dict(checkpoint['model_state_dict_G'])
        optimizer_D.load_state_dict(checkpoint['optimizer_state_dict_D'])
        optimizer_G.load_state_dict(checkpoint['optimizer_state_dict_G'])

    # load scheduler
    scheduler_G = None
    scheduler_D = None
    if glv.network_config["is_scheduler"]:
        scheduler_D = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer_D,
                                                                 T_max=20)
        scheduler_G = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer_G,
                                                                 T_max=20)

    logging.info(glv.network_config)

    print("start training")
    # batch_size = glv.network_config['batch_size']
    img_size = next(iter(trainloader))[0].shape[-1]
    channels = 2
    for epoch in range(init_epoch, glv.network_config['epochs']):
        net_G.train()
        fake_mean = 0
        real_mean = 0
        g_mean = 0
        batch_count = 0
        g_count = 0
        for X, _ in tqdm(trainloader, colour='blue'):
            batch_count += 1
            batch_size = X.shape[0]
            if not glv.network_config['net_D_direct_input']:
                X = X.reshape((batch_size, -1))
            X = X.to(device)
            Z = torch.randn((batch_size, glv.network_config['latent_dim']),
                            device=device)
            mean_increment_fake, mean_increment_real = update_D(
                X, Z, net_D, net_G, optimizer_D, img_size, channels)
            # for parm in net_D.parameters():
            # parm.data.clamp_((-1)*glv.network_config['clamp_num'],glv.network_config['clamp_num'])
            fake_mean += mean_increment_fake
            real_mean += mean_increment_real
            reset_net(net_D)
            reset_net(net_G)
            #if batch_count % glv.network_config['n_critic'] == 0:
            mean_increment_g = update_G(Z, net_D, net_G, optimizer_G, img_size,
                                        channels)
            g_count += 1
            g_mean += mean_increment_g
            reset_net(net_D)
            reset_net(net_G)

        #eta_G, eta_D = -1, -1
        if glv.network_config['is_scheduler']:
            #eta_D = scheduler_D.get_last_lr()
            #eta_G = scheduler_G.get_last_lr()
            scheduler_D.step()
            scheduler_G.step()

        with torch.no_grad():
            net_G.eval()
            Z = torch.randn((21, glv.network_config['latent_dim']),
                            device=device)
            fake_X = net_G(Z)  # fake_X.shape = (n_steps, batch_size, 784)
            reset_net(net_G)

            # decode for dvs fake_X
            method = 'none'
            fake_X = dvs_decode(fake_X, method=method)

            # save imgs or gifs
            if method == 'none':
                # have time dim
                fake_X = fake_X.reshape(fake_X.shape[0], 21, channels,
                                        img_size, img_size)
                fake_X = fake_X.transpose(0, 1)  # (B,T,C,H,W)

                imgs = torch.cat([
                    torch.cat(
                        [fake_X[i * 7 + j, :, :, :, :] for j in range(7)],
                        dim=3) for i in range(3)
                ],
                                 dim=2)
                # print(imgs.shape)
                print(torch.min(imgs), torch.max(imgs))
                play_frame(
                    imgs,
                    save_gif_to=f'./exp_results/images/{name}/Epoch{epoch}.gif'
                )

            else:
                # have no time dim
                fake_X = fake_X.reshape(21, channels, img_size, img_size)
                save_image(fake_X,
                           'f./exp_results/images/{name}/Epoch{epoch}.png',
                           nrow=3)
            '''fake_X = fake_X.reshape((21, channels, img_size, img_size))
            imgs = torch.cat([
                torch.cat([fake_X[i * 7 + j, :, :, :] for j in range(7)],
                          dim=2) for i in range(3)
            ],
                             dim=1)
            # imgs = imgs * 255.0
            save_image(imgs, f'./exp_results/images/{name}/Epoch{epoch}.png')'''
            if (epoch + 1) % glv.network_config['save_every'] == 0:
                torch.save(
                    {
                        'epoch': epoch,
                        'model_state_dict_D': net_D.state_dict(),
                        'model_state_dict_G': net_G.state_dict(),
                        'optimizer_state_dict_D': optimizer_D.state_dict(),
                        'optimizer_state_dict_G': optimizer_G.state_dict()
                    }, f'./exp_results/checkpoints/{name}_{epoch+1}.pth')
        logging.info(
            f'Epoch: {epoch}'
            f'fake_credit:{fake_mean / batch_count},'
            f'real_credit:{real_mean / batch_count}, g_credit:{g_mean / g_count}'
        )
        print(
            f'Epoch: {epoch}'
            f'fake_credit:{fake_mean / batch_count},'
            f'real_credit:{real_mean / batch_count}, g_credit:{g_mean / g_count}'
        )

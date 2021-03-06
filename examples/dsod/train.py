from __future__ import print_function
import matplotlib

matplotlib.use('agg')
import os
import random

import torch
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn

import torchvision.transforms as transforms
from PIL import Image, ImageDraw
from torch.autograd import Variable

from torchcv.models.ssd import SSDBoxCoder
import numpy as np
from torchcv.loss import SSDLoss
from torchcv.datasets import ListDataset
from torchcv.transforms import resize, random_flip, random_paste, random_crop, random_distort
from torchcv.models import DSOD
from torchcv.evaluations.voc_eval import voc_eval
from tqdm import tqdm
from torchcv.visualizations import Visualizer

from torchcv.utils.config import opt
from torchnet.meter import AverageValueMeter

def Transform(box_coder, train=True):
    def train_(img, boxes, labels):
        img = random_distort(img)
        if random.random() < 0.5:
            img, boxes = random_paste(img, boxes, max_ratio=4, fill=(123, 116, 103))
        img, boxes, labels = random_crop(img, boxes, labels)
        img, boxes = resize(img, boxes, size=(opt.img_size, opt.img_size), random_interpolation=True)
        img, boxes = random_flip(img, boxes)
        img = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
        ])(img)
        boxes, labels = box_coder.encode(boxes, labels)
        return img, boxes, labels

    def test_(img, boxes, labels):
        img, boxes = resize(img, boxes, size=(opt.img_size, opt.img_size))
        img = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
        ])(img)
        boxes, labels = box_coder.encode(boxes, labels)
        return img, boxes, labels

    return train_ if train else test_


def eval(net,test_num=10000):
    net.eval()

    def transform(img, boxes, labels):
        img, boxes = resize(img, boxes, size=(opt.img_size, opt.img_size))
        img = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
        ])(img)
        return img, boxes, labels

    dataset = ListDataset(root=opt.data_root, \
                          list_file=opt.voc07_test,
                          transform=transform)
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=1, shuffle=False, num_workers=8)
    box_coder = SSDBoxCoder(net)

    pred_boxes = []
    pred_labels = []
    pred_scores = []
    gt_boxes = []
    gt_labels = []

    with open('torchcv/datasets/voc/voc07_test_difficult.txt') as f:
        gt_difficults = []
        for line in f.readlines():
            line = line.strip().split()
            d = np.array([int(x) for x in line[1:]])
            gt_difficults.append(d)

    for i, (inputs, box_targets, label_targets) in tqdm(enumerate(dataloader)):
        gt_boxes.append(box_targets.squeeze(0))
        gt_labels.append(label_targets.squeeze(0))

        loc_preds, cls_preds = net(Variable(inputs.cuda(), volatile=True))
        box_preds, label_preds, score_preds = box_coder.decode(
            loc_preds.cpu().data.squeeze(),
            F.softmax(cls_preds.squeeze(), dim=1).cpu().data,
            score_thresh=0.01)

        pred_boxes.append(box_preds)
        pred_labels.append(label_preds)
        pred_scores.append(score_preds)
        if i==test_num:break

    aps = (voc_eval(
        pred_boxes, pred_labels, pred_scores,
        gt_boxes, gt_labels, gt_difficults,
        iou_thresh=0.5, use_07_metric=True))
    net.train()
    return aps


def predict(net, box_coder, img):
    if isinstance(img, str):
        img = Image.open(img)
        ow = oh = 300
        img = img.resize((ow, oh))
    transform = transforms.Compose([
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225))
    ])
    x = transform(img).cuda()
    x = Variable(x, volatile=True)
    loc_preds, cls_preds = net(x.unsqueeze(0))
    try:
        boxes, labels, scores = box_coder.decode(
            loc_preds.data.cpu().squeeze(), F.softmax(cls_preds.squeeze().cpu(), dim=1).data)
    except:print('except in predict')
    draw = ImageDraw.Draw(img)
    for box in boxes:
        draw.rectangle(list(box), outline='red')
    return img


def main(**kwargs):
    opt._parse(kwargs)

    vis = Visualizer(env=opt.env)

    # Model
    print('==> Building model..')
    net = DSOD(num_classes=21)
    start_epoch = 0  # start from epoch 0 or last epoch

    if opt.load_path is not None:
        print('==> Resuming from checkpoint..')
        checkpoint = torch.load(opt.load_path)
        net.load_state_dict(checkpoint['net'])

    # Dataset
    print('==> Preparing dataset..')
    box_coder = SSDBoxCoder(net)

    trainset = ListDataset(root=opt.data_root,
                           list_file=[opt.voc07_trainval, opt.voc12_trainval],
                           transform=Transform(box_coder, True))

    trainloader = torch.utils.data.DataLoader(trainset, batch_size=opt.batch_size, shuffle=True, num_workers=8)

    net.cuda()
    net = torch.nn.DataParallel(net, device_ids=range(torch.cuda.device_count()))
    cudnn.benchmark = True

    criterion = SSDLoss(num_classes=21)
    optimizer = optim.SGD(net.parameters(), lr=opt.lr, momentum=0.9, weight_decay=5e-4)

    best_map_ = 0
    for epoch in range(start_epoch, start_epoch + 200):
        print('\nEpoch: %d' % epoch)
        net.train()
        train_loss = 0
        for batch_idx, (inputs, loc_targets, cls_targets) in tqdm(enumerate(trainloader)):
            inputs = Variable(inputs.cuda())
            loc_targets = Variable(loc_targets.cuda())
            cls_targets = Variable(cls_targets.cuda())

            optimizer.zero_grad()
            loc_preds, cls_preds = net(inputs)
            loss = criterion(loc_preds, loc_targets, cls_preds, cls_targets)
            loss.backward()
            optimizer.step()

            train_loss += loss.data[0]
            if (batch_idx + 1) % opt.plot_every == 0:
                vis.plot('loss', train_loss / (batch_idx + 1))

                img = predict(net, box_coder, os.path.join(opt.data_root, trainset.fnames[batch_idx]))
                vis.img('predict', np.array(img).transpose(2, 0, 1))

                if os.path.exists(opt.debug_file):
                    import ipdb
                    ipdb.set_trace()

        aps = eval(net.module,test_num=epoch*100+100)
        map_ = aps['map']
        if map_ > best_map_:
            print('Saving..')
            state = {
                'net': net.state_dict(),
                'map': best_map_,
                'epoch': epoch,
            }
            best_map_ = map_
            if not os.path.isdir(os.path.dirname(opt.checkpoint)):
                os.mkdir(os.path.dirname(opt.checkpoint))
            torch.save(state, opt.checkpoint + '/%s.pth' % best_map_)


if __name__ == '__main__':
    import fire

    fire.Fire()

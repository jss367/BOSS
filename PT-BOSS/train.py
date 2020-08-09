import random
import time
import argparse
import sys

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F

from model import WideResnet
from cifar import get_train_loader, get_val_loader, OneHot
from label_guessor import LabelGuessor
from lr_scheduler import WarmupCosineLrScheduler
from ema import EMA


## args
parser = argparse.ArgumentParser(description=' FixMatch Training')
parser.add_argument('--wresnet-k', default=2, type=int,
                    help='width factor of wide resnet')
parser.add_argument('--wresnet-n', default=28, type=int,
                    help='depth of wide resnet')
parser.add_argument('--n-classes', type=int, default=10,
                    help='number of classes in dataset')
parser.add_argument('--n-labeled', type=int, default=40,
                    help='number of labeled samples for training')
parser.add_argument('--n-epochs', type=int, default=1024,
                    help='number of training epochs')
parser.add_argument('--batchsize', type=int, default=64,
                    help='train batch size of labeled samples')
parser.add_argument('--mu', type=int, default=7,
                    help='factor of train batch size of unlabeled samples')
parser.add_argument('--thr', type=float, default=0.95,
                    help='pseudo label threshold')
parser.add_argument('--n-imgs-per-epoch', type=int, default=64 * 1024,
                    help='number of training images for each epoch')
parser.add_argument('--lam-u', type=float, default=1.,
                    help='coefficient of unlabeled loss')
parser.add_argument('--lam-c', type=float, default=0.0,
                    help='coefficient of class number STD loss')
parser.add_argument('--ema-alpha', type=float, default=0.999,
                    help='decay rate for ema module')
parser.add_argument('--lr', type=float, default=0.03,
                    help='learning rate for training')
parser.add_argument('--weight-decay', type=float, default=5e-4,
                    help='weight decay')
parser.add_argument('--momentum', type=float, default=0.9,
                    help='momentum for optimizer')
parser.add_argument('--seed', type=int, default=1,
                    help='Which set of labeled data to use. ')
#                    help='seed for random behaviors, no seed if negtive. ')
parser.add_argument('--balance', type=int, default=0,
                     help='Balance class methods to use (default=0 None)')
parser.add_argument('--delT', type=float, default=0.2,
                     help='Class balance threshold delta (default=0.2)')

args = parser.parse_args()
print(args)

## settings
#  torch.multiprocessing.set_sharing_strategy('file_system')
#if args.seed > 0:
#    torch.manual_seed(args.seed)
#    random.seed(args.seed)
#    np.random.seed(args.seed)
#    torch.backends.cudnn.deterministic = True


def set_model():
    model = WideResnet(
        args.n_classes, k=args.wresnet_k, n=args.wresnet_n) # wresnet-28-2
    model.train()
    model.cuda()
    criteria_x = nn.CrossEntropyLoss().cuda()
    criteria_u = nn.CrossEntropyLoss().cuda()
    return model, criteria_x, criteria_u


def train_one_epoch(
        model,
        criteria_x,
        criteria_u,
        optim,
        lr_schdlr,
        ema,
        dltrain_x,
        dltrain_u,
        lb_guessor,
        lambda_u,
        lambda_c,
        n_iters,
    ):
    loss_avg, loss_x_avg, loss_u_avg, loss_u_real_avg = [], [], [], []
    loss_u_std = 0
    n_correct_lbs = []
    st = time.time()
    dl_x, dl_u = iter(dltrain_x), iter(dltrain_u)
    n_strong = 0 
    for it in range(n_iters):
        ims_x_weak, ims_x_strong, lbs_x = next(dl_x)
        ims_u_weak, ims_u_strong, lbs_u_real = next(dl_u)
        
        ims_x_strong = ims_x_strong.cuda()
        ims_x_weak = ims_x_weak.cuda()
        lbs_x = lbs_x.type(torch.LongTensor).cuda()
        ims_u_weak = ims_u_weak.cuda()
        ims_u_strong = ims_u_strong.cuda()

#        lbs_u, valid_u, mask_u = lb_guessor(model, ims_u_weak)
        lbs_u, valid_u, mask_u, loss_u_std  = lb_guessor(model, ims_u_weak, args.balance, args.delT)
        ims_u_strong = ims_u_strong[valid_u]
        n_x, n_u = ims_x_weak.size(0), ims_u_strong.size(0)
        if n_u != 0:
            ims_x_u = torch.cat([ims_x_weak, ims_u_strong], dim=0).detach()
            lbs_x_u = torch.cat([lbs_x, lbs_u], dim=0).detach()
            logits_x_u = model(ims_x_u)
            logits_x, logits_u = logits_x_u[:n_x], logits_x_u[n_x:]
            loss_x = criteria_x(logits_x, lbs_x)
            if args.balance == 2 or args.balance == 3:
                loss_u = (F.cross_entropy(logits_u, lbs_u, reduction='none') * mask_u).mean()
            else:
                loss_u = criteria_u(logits_u, lbs_u)

            loss = loss_x + lambda_u * loss_u + lambda_c * loss_u_std
            with torch.no_grad():
                lbs_u_real = lbs_u_real[valid_u].cuda()
                corr_lb = lbs_u_real == lbs_u
                loss_u_real = F.cross_entropy(logits_u, lbs_u_real.type(torch.LongTensor).cuda())
        else:
            logits_x = model(ims_x_weak)
            loss_x = criteria_x(logits_x, lbs_x)
            loss_u = torch.tensor(0)
            loss_u_real = torch.tensor(0)
            corr_lb = torch.tensor(0)
            loss = loss_x

        optim.zero_grad()
        loss.backward()
        optim.step()
        ema.update_params()
        lr_schdlr.step()

        loss_avg.append(loss.item())
        loss_x_avg.append(loss_x.item())
        loss_u_avg.append(loss_u.item())
        loss_u_real_avg.append(loss_u_real.item())
        n_correct_lbs.append(corr_lb.sum().item())
        n_strong += n_u

        if (it+1) % 512 == 0:
            ed = time.time()
            t = ed -st
            loss_avg = sum(loss_avg) / len(loss_avg)
            loss_x_avg = sum(loss_x_avg) / len(loss_x_avg)
            loss_u_avg = sum(loss_u_avg) / len(loss_u_avg)
            loss_u_real_avg = sum(loss_u_real_avg) / len(loss_u_real_avg)
            n_correct_lbs = sum(n_correct_lbs) / len(n_correct_lbs)
            lr_log = [pg['lr'] for pg in optim.param_groups]
            lr_log = sum(lr_log) / len(lr_log)
            n_strong /= 512
            msg = ', '.join([
                'iter: {}',
                'loss: {:.4f}',
                'loss_u: {:.4f}',
                'loss_u_std: {:.4f}',
                'loss_x: {:.4f}',
                'loss_u_real_lb: {:.4f}',
                'n_correct_u: {}/{}',
                'lr: {:.4f}',
                'time: {:.2f}',
            ]).format(
                it+1, loss_avg, loss_u, loss_u_std, loss_x, loss_u_real_avg,
                int(n_correct_lbs), int(n_strong), lr_log, t
            )
            loss_avg, loss_x_avg, loss_u_avg, loss_u_real_avg = [], [], [], []
            n_correct_lbs = []
            st = ed
            n_strong = 0
            print(msg)
#            print(len(ims_u_weak),len(ims_u_strong),len(lbs_u_real))
#            printCounts(model, ims_u_weak, args.balance)

    ema.update_buffer()

def printCounts(model, ims, balance):
    with torch.no_grad():
#        model.train()
        logits = model(ims)
        probs = torch.softmax(logits, dim=1)
        scores, lbs = torch.max(probs, dim=1)
        labels, count_unlabels = np.unique(lbs.cpu(),return_counts=True)
        if balance > 0:
            mxCount = max(count_unlabels) + 1
            ratios =  [x/mxCount for x in count_unlabels]
            idx = (lbs == -1)
            for i in range(len(labels)):
                idx = idx | ((scores*(lbs==labels[i]).float()) > (args.thr - 0.2*(1-ratios[i])))

            lbs = lbs[idx]
            labels, count_unlabels = np.unique(lbs.cpu(),return_counts=True)

#        print("count_unlabels ", count_unlabels)

def evaluate(ema):
    ema.apply_shadow()
    ema.model.eval()
    ema.model.cuda()

    dlval = get_val_loader(batch_size=128, num_workers=0, root='cifar10')
    matches = []
    for ims, lbs in dlval:
        ims = ims.cuda()
        lbs = lbs.cuda()
        with torch.no_grad():
            logits = ema.model(ims)
            scores = torch.softmax(logits, dim=1)
            _, preds = torch.max(scores, dim=1)
            match = lbs == preds
            matches.append(match)
#            printCounts(ema.model, ims, args.balance)
    matches = torch.cat(matches, dim=0).float()
    acc = torch.mean(matches)
    ema.restore()
    return acc

def sort_unlabeled(ema):
    ema.apply_shadow()
    ema.model.eval()
    ema.model.cuda()

    dltrain_x, dltrain_u = get_train_loader(
        10, 2000, 1, L=args.n_labeled, seed=args.seed)
    matches = []
    for ims_w, ims_s, lbs in  dltrain_u:
        ims = ims_w.cuda()
        with torch.no_grad():
            logits = ema.model(ims)
            scores = torch.softmax(logits, dim=1)
            predictions , preds = torch.max(scores, dim=1)
            top = torch.argsort(predictions, descending=True).cpu()
    preds = preds.cpu()
    predictions = predictions.cpu()
#    print(predictions[top[0:100]])
#    print(preds[top[0:100]])
#    print(top[0:1000])
    name = "dataset/pseudolabels/top/top_preds"+"cifar10pB"+str(args.balance)+"."+str(args.seed)
    name = name+"WD"+str(args.weight_decay)+"LR"+str(args.lr)+"DT"+str(args.delT)+"T"+str(args.thr)
    np.save(name ,top[0:1000])
    name = "dataset/pseudolabels/top/top_labels"+"cifar10pB"+str(args.balance)+"."+str(args.seed)
    name = name+"WD"+str(args.weight_decay)+"LR"+str(args.lr)+"DT"+str(args.delT)+"T"+str(args.thr)
    np.save(name ,preds[top[0:1000]])
    ema.restore()
    return 

def train():
    n_iters_per_epoch = args.n_imgs_per_epoch // args.batchsize
    n_iters_all = n_iters_per_epoch * args.n_epochs

    model, criteria_x, criteria_u = set_model()

    dltrain_x, dltrain_u = get_train_loader(
        args.batchsize, args.mu, n_iters_per_epoch, L=args.n_labeled, seed=args.seed)
    lb_guessor = LabelGuessor(thresh=args.thr)

    ema = EMA(model, args.ema_alpha)

    wd_params, non_wd_params = [], []
    for param in model.parameters():
        if len(param.size()) == 1:
            non_wd_params.append(param)
        else:
            wd_params.append(param)
    param_list = [
        {'params': wd_params}, {'params': non_wd_params, 'weight_decay': 0}]
    optim = torch.optim.SGD(param_list, lr=args.lr, weight_decay=args.weight_decay,
        momentum=args.momentum, nesterov=True)
    lr_schdlr = WarmupCosineLrScheduler(
        optim, max_iter=n_iters_all, warmup_iter=0
    )

    train_args = dict(
        model=model,
        criteria_x=criteria_x,
        criteria_u=criteria_u,
        optim=optim,
        lr_schdlr=lr_schdlr,
        ema=ema,
        dltrain_x=dltrain_x,
        dltrain_u=dltrain_u,
        lb_guessor=lb_guessor,
        lambda_u=args.lam_u,
        lambda_c=args.lam_c,
        n_iters=n_iters_per_epoch,
    )
    best_acc = -1
    print('start to train')
    for e in range(args.n_epochs):
        model.train()
        print('epoch: {}'.format(e+1))
        train_one_epoch(**train_args)
        torch.cuda.empty_cache()

        acc = evaluate(ema)
        best_acc = acc if best_acc < acc else best_acc
        log_msg = [
            'epoch: {}'.format(e),
            'acc: {:.4f}'.format(acc),
            'best_acc: {:.4f}'.format(best_acc)]
        print(', '.join(log_msg))

    sort_unlabeled(ema)

if __name__ == '__main__':
    train()

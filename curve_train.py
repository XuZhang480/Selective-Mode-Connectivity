import argparse
import os
import sys
import tabulate
import time
import torch
import torch.nn.functional as F
import numpy as np

import attack.pgd as pgd
import attack.pgd2 as pgd2
from attack.att import *
from attack.autopgd_train import apgd_train,pgd_1

import curves
import data
import models
import utils

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='DNN curve training')
    parser.add_argument('--dir', type=str, default='/tmp/curve/', metavar='DIR',
                        help='training directory (default: /tmp/curve/)')

    parser.add_argument('--dataset', type=str, default='CIFAR10', metavar='DATASET',
                        help='dataset name (default: CIFAR10)')
    parser.add_argument('--use_test', action='store_true',
                        help='switches between validation and test set (default: validation)')
    parser.add_argument('--random_select', action='store_true',
                        help='randomly select weights by ratio (default: false)')
    parser.add_argument('--transform', type=str, default='VGG', metavar='TRANSFORM',
                        help='transform name (default: VGG)')
    parser.add_argument('--data_path', type=str, default=None, metavar='PATH',
                        help='path to datasets location (default: None)')
    parser.add_argument('--batch_size', type=int, default=128, metavar='N',
                        help='input batch size (default: 128)')
    parser.add_argument('--gpus', type=str, default='',
                        help='GPUS')
    parser.add_argument('--num-workers', type=int, default=4, metavar='N',
                        help='number of workers (default: 4)')
    parser.add_argument('--model', type=str, default=None, metavar='MODEL', required=True,
                        help='model name (default: None)')

    parser.add_argument('--curve', type=str, default=None, metavar='CURVE',
                        help='curve type to use (default: None)')
    parser.add_argument('--num_bends', type=int, default=3, metavar='N',
                        help='number of curve bends (default: 3)')
    parser.add_argument('--init_start', type=str, default=None, metavar='CKPT',
                        help='checkpoint to init start point (default: None)')
    parser.add_argument('--fix_start', dest='fix_start', action='store_true',
                        help='fix start point (default: off)')
    parser.add_argument('--init_end', type=str, default=None, metavar='CKPT',
                        help='checkpoint to init end point (default: None)')
    parser.add_argument('--fix_end', dest='fix_end', action='store_true',
                        help='fix end point (default: off)')
    parser.set_defaults(init_linear=True)
    parser.add_argument('--init_linear_off', dest='init_linear', action='store_false',
                        help='turns off linear initialization of intermediate points (default: on)')
    parser.add_argument('--resume', type=str, default=None, metavar='CKPT',
                        help='checkpoint to resume training from (default: None)')

    parser.add_argument('--epochs', type=int, default=200, metavar='N',
                        help='number of epochs to train (default: 200)')
    parser.add_argument('--save_freq', type=int, default=50, metavar='N',
                        help='save frequency (default: 50)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='initial learning rate (default: 0.01)')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--wd', type=float, default=1e-4, metavar='WD',
                        help='weight decay (default: 1e-4)')

    parser.add_argument('--seed', type=int, default=1, metavar='S', help='random seed (default: 1)')

    parser.add_argument('--pgd',type=str,default=0,metavar='PGD',help='type of pgd attack')

    parser.add_argument('--R', type=float, default=5, metavar='ROUNDS R', help='rounds of weight selection')

    parser.add_argument('--k', type=float, default=0.5, metavar='RATIO k', help='ratio of parameters to be updated')

    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    with open(os.path.join(args.dir, 'command.sh'), 'w') as f:
        f.write(' '.join(sys.argv))
        f.write('\n')

    torch.backends.cudnn.benchmark = True
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    loaders, num_classes = data.loaders(
        args.dataset,
        args.data_path,
        args.batch_size,
        args.num_workers,
        args.transform,
        args.use_test
    )

    architecture = getattr(models, args.model)
    if args.dataset == 'ImageNet100' and 'PreResNet' in args.model:
        architecture.kwargs['imgdim']=49
    curveflag=False
    if args.curve is None:
        model = architecture.base(num_classes=num_classes, **architecture.kwargs)
    else:
        curveflag=True
        curve = getattr(curves, args.curve)
        model = curves.CurveNet(
            num_classes,
            curve,
            architecture.curve,
            args.num_bends,
            args.fix_start,
            args.fix_end,
            architecture_kwargs=architecture.kwargs,
        )
        base_model = None
        if args.resume is None:
            for path, k in [(args.init_start, 0), (args.init_end, args.num_bends - 1)]:
                if path is not None:
                    if base_model is None:
                        base_model = architecture.base(num_classes=num_classes, **architecture.kwargs)
                    checkpoint = torch.load(path)
                    print('Loading %s as point #%d' % (path, k))
                    '''
                    base_model.load_state_dict(checkpoint['model_state'])
                    '''
                    weights = checkpoint['model_state']
                    weights_dict = {}
                    for key, value in weights.items():
                        new_k = key.replace('module.', '') if 'module.' in key  else key
                        weights_dict[new_k] = value
                    base_model.load_state_dict(weights_dict)
                    model.import_base_parameters(base_model, k)
            if args.init_linear:
                print('Linear initialization.')
                model.init_linear()
    model.cuda()
    if len(args.gpus)>0:
        gpus=list(map(int,args.gpus.split(',')))
        model=torch.nn.DataParallel(model,device_ids=gpus)

    def learning_rate_schedule(base_lr, epoch, total_epochs):
        alpha = epoch / total_epochs
        if alpha <= 0.5:
            factor = 1.0
        elif alpha <= 0.9:
            factor = 1.0 - (alpha - 0.5) / 0.4 * 0.99
        else:
            factor = 0.01
        return factor * base_lr


    criterion = F.cross_entropy
    regularizer = None if args.curve is None else curves.l2_regularizer(args.wd)
    optimizer = torch.optim.SGD(
        filter(lambda param: param.requires_grad, model.parameters()),
        lr=args.lr,
        momentum=args.momentum,
        weight_decay=args.wd if args.curve is None else 0.0
    )

    start_epoch = 1
    if args.resume is not None:
        print('Resume training from %s' % args.resume)
        checkpoint = torch.load(args.resume)
        start_epoch = checkpoint['epoch'] + 1
        model.load_state_dict(checkpoint['model_state'])
        optimizer.load_state_dict(checkpoint['optimizer_state'])

    if curveflag:
        columns = ['ep', 'lr', 'tr_t', 'tr_loss', 'tr_acc', 'te_t', 'te_nll', 'te_acc', 'time']

    else:
        columns = ['ep', 'lr', 'tr_loss', 'tr_acc', 'te_nll', 'te_acc', 'time']

    utils.save_checkpoint(
        args.dir,
        start_epoch - 1,
        model_state=model.state_dict(),
        optimizer_state=optimizer.state_dict()
    )

    weight_selection_rounds = [i * (args.epochs // args.R) + 1 for i in range(args.R)]

    import torch
    import numpy as np


    def DPU(train_loader, model, criterion, optimizer, regularizer=None, pgdtype='0', curveflag=False, k=0.5, random_select=False):
        grad_scores = []
        param_list = []

        model.train()
        train_iter = iter(train_loader)
        input, target = next(train_iter)
        input = input.cuda()
        target = target.cuda()

        t = input.data.new(1).uniform_()

        if pgdtype == 'inf':
            at = pgd.PGD()
            if curveflag:
                input = at.generate(model, input, target, None, 0, t=t)
            else:
                input = at.generate(model, input, target, None, 0)
            model.train()
        if pgdtype == '2':
            at = pgd2.PGD()
            if curveflag:
                input = at.generate(model, input, target, None, 0, t=t)
            else:
                input = at.generate(model, input, target, None, 0)
            model.train()
        if pgdtype == '1':
            model.eval()
            if curveflag:
                input += pgd_l1_topk(model, input, target, epsilon=12, alpha=0.05, num_iter=10, device="cuda:0",
                                     restarts=0, version=0, t=t)
            else:
                input, acc_tr, _, _ = apgd_train(model, input, target, norm='L1', eps=12, n_iter=10)
            model.train()
        if pgdtype == 'msd':
            if curveflag:
                input += msd_v0(model, input, target, epsilon_l_inf=2 / 255, epsilon_l_2=1, epsilon_l_1=12,
                                alpha_l_inf=2 / 255, alpha_l_2=0.2, alpha_l_1=0.05, num_iter=10, device="cuda:0", t=t)
            else:
                input += msd_v0(model, input, target, epsilon_l_inf=8 / 255, epsilon_l_2=1, epsilon_l_1=12,
                                alpha_l_inf=2 / 255, alpha_l_2=0.2, alpha_l_1=0.05, num_iter=10, device="cuda:0")
            model.train()

        output = model(**dict(input=input, t=float(t.item()))) if curveflag else model(input)
        loss = criterion(output, target)
        if regularizer is not None:
            loss += regularizer(model)

        model.zero_grad()
        loss.backward()

        for param in model.parameters():
            if param.requires_grad and param.grad is not None:
                grad_score = param.grad.pow(2).sum().item()
                grad_scores.append(grad_score)
                param_list.append(param)

        if len(grad_scores) == 0:
            return optimizer

        if random_select:
            num_selected = int(len(param_list) * k)
            selected_indices = np.random.choice(len(param_list), num_selected, replace=False)
            selected_params = [param_list[i] for i in selected_indices]
        else:
            threshold = np.percentile(grad_scores, (1 - k) * 100)
            selected_params = [param for param, score in zip(param_list, grad_scores) if score >= threshold]

        selected_param_set = set(selected_params)
        for param in model.parameters():
            param.requires_grad = param in selected_param_set

        optimizer_defaults = optimizer.defaults
        optimizer_cls = type(optimizer)

        new_optimizer = optimizer_cls(selected_params, **optimizer_defaults)

        return new_optimizer

    has_bn = utils.check_bn(model)
    test_res = {'loss': None, 'accuracy': None, 'nll': None}
    for epoch in range(start_epoch, args.epochs + 1):
        time_ep = time.time()

        if epoch in weight_selection_rounds:
            optimizer = DPU(loaders['train'], model, criterion, optimizer, regularizer, pgdtype=args.pgd, curveflag=curveflag, k=args.k)

        lr = learning_rate_schedule(args.lr, epoch, args.epochs)
        utils.adjust_learning_rate(optimizer, lr)

        train_res = utils.train(loaders['train'], model, optimizer, criterion, regularizer, pgdtype=args.pgd, curveflag=curveflag)
        test_res = utils.test(loaders['test'], model, criterion, regularizer, pgdtype=args.pgd, curveflag=curveflag)
        # if args.curve is None or not has_bn:
        #     test_res = utils.test(loaders['test'], model, criterion, regularizer, pgdtype=args.pgd,curveflag=curveflag)

        if epoch % args.save_freq == 0:
            utils.save_checkpoint(
                args.dir,
                epoch,
                model_state=model.state_dict(),
                optimizer_state=optimizer.state_dict()
            )

        time_ep = time.time() - time_ep
        if curveflag:
            values = [epoch, lr, train_res['t'], train_res['loss'], train_res['accuracy'], test_res['t'],
                      test_res['nll'],
                      test_res['accuracy'], time_ep]
        else:
            values = [epoch, lr, train_res['loss'], train_res['accuracy'], test_res['nll'],
                      test_res['accuracy'], time_ep]

        table = tabulate.tabulate([values], columns, tablefmt='simple', floatfmt='9.4f')
        if epoch % 40 == 1 or epoch == start_epoch:
            table = table.split('\n')
            table = '\n'.join([table[1]] + table)
        else:
            table = table.split('\n')[2]
        print(table)

    if args.epochs % args.save_freq != 0:
        utils.save_checkpoint(
            args.dir,
            args.epochs,
            model_state=model.state_dict(),
            optimizer_state=optimizer.state_dict()
        )

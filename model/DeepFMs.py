# -*- coding:utf-8 -*-


"""
Created on Dec 10, 2017
@author: jachin,Nie

Edited by Wei Deng on Jun.7, 2019

A pytorch implementation of deepfms including: FM, FFM, FwFM, DeepFM, DeepFFM, DeepFwFM

Reference:
[1] DeepFwFM: A Factorization-Machine based Neural Network for CTR Prediction,
    Huifeng Guo, Ruiming Tang, Yunming Yey, Zhenguo Li, Xiuqiang He.

"""

import os, sys, random
import numpy as np
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.metrics import roc_auc_score, precision_recall_curve, auc, log_loss
from time import time
import math
import logging

import torch
import torch.autograd as autograd
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable

import torch.backends.cudnn
from torch.nn.quantized import QFunctional, DeQuantize
from torch.quantization import QuantStub, DeQuantStub

from model.MDEmbeddingBag import PrEmbeddingBag
from model.QREmbeddingBag import QREmbeddingBag

"""
    Network structure
"""


class DeepFMs(torch.nn.Module):
    """
    :parameter
    -------------
    field_size: size of the feature fields
    feature_sizes: a field_size-dim array, sizes of the feature dictionary
    embedding_size: size of the feature embedding
    is_shallow_dropout: bool, shallow part(fm or ffm part) uses dropout or not?
    dropout_shallow: an array of the size of 2, example:[0.5,0.5], the first element is for the-first order part and the second element is for the second-order part
    h_depth: deep network's hidden layers' depth
    deep_layers: a h_depth-dim array, each element is the size of corresponding hidden layers. example:[32,32] h_depth = 2
    is_deep_dropout: bool, deep part uses dropout or not?
    dropout_deep: an array of dropout factors,example:[0.5,0.5,0.5] h_depth=2
    deep_layers_activation: relu or sigmoid etc
    n_epochs: epochs
    batch_size: batch_size
    learning_rate: learning_rate
    optimizer_type: optimizer_type, 'adam', 'rmsp', 'sgd', 'adag'
    is_batch_norm：bool,  use batch_norm or not ?
    verbose: verbose
    weight_decay: weight decay (L2 penalty)
    use_fm: bool
    use_ffm: bool
    use_deep: bool
    loss_type: "logloss", only, "softmax" for KD needed?
    eval_metric: roc_auc_score
    use_cuda: bool use gpu or cpu?
    n_class: number of classes. is bounded to 1
    greater_is_better: bool. Is the greater eval better?


    Attention: only support logsitcs regression
    """

    def __init__(self, field_size, feature_sizes, embedding_size=10, is_shallow_dropout=True, dropout_shallow=[0.0, 0.0],
                 h_depth=3, deep_nodes=400, is_deep_dropout=True, dropout_deep=[0.5, 0.5, 0.5, 0.5],
                 eval_metric=roc_auc_score,
                 deep_layers_activation='relu', n_epochs=64, batch_size=2048, learning_rate=0.001, momentum=0.9,
                 optimizer_type='adam', is_batch_norm=False, verbose=False, random_seed=0, weight_decay=0.0,
                 use_fm=True, use_fwlw=False, use_lw=False, use_ffm=False, use_fwfm=False, use_deep=True,
                 loss_type='logloss',
                 use_cuda=True, n_class=1, greater_is_better=True, sparse=0.9, warm=10, num_deeps=1, numerical=13,
                 use_logit=0, embedding_bag=False, quantization_aware=False, dynamic_quantization=False, static_quantization=False, static_calibrate=False,
                 qr_flag=0, qr_operation="mult", qr_collisions=1, qr_threshold=200, md_flag=0, md_threshold=200):
        super(DeepFMs, self).__init__()
        self.field_size = field_size
        self.feature_sizes = feature_sizes
        self.embedding_size = embedding_size
        self.is_shallow_dropout = is_shallow_dropout
        self.dropout_shallow = dropout_shallow
        self.h_depth = h_depth
        self.num_deeps = num_deeps
        self.deep_layers = [deep_nodes] * h_depth
        self.is_deep_dropout = is_deep_dropout
        self.dropout_deep = dropout_deep
        self.deep_layers_activation = deep_layers_activation
        self.n_epochs = n_epochs
        self.batch_size = batch_size
        self.learning_rate = learning_rate
        self.momentum = momentum
        self.optimizer_type = optimizer_type
        self.is_batch_norm = is_batch_norm
        self.verbose = verbose
        self.weight_decay = weight_decay
        self.random_seed = random_seed
        self.use_fm = use_fm
        self.use_fwlw = use_fwlw
        self.use_lw = use_lw
        self.use_ffm = use_ffm
        self.use_fwfm = use_fwfm
        self.use_logit = use_logit
        self.use_deep = use_deep
        self.loss_type = loss_type
        self.eval_metric = eval_metric
        self.use_cuda = use_cuda
        self.n_class = n_class
        self.greater_is_better = greater_is_better
        self.target_sparse = sparse
        self.warm = warm
        self.num = numerical
        self.embedding_bag = embedding_bag
        self.quantization_aware = quantization_aware
        self.static_quantization = static_quantization
        self.static_calibrate = static_calibrate
        self.dynamic_quantization = dynamic_quantization
        self.qr_flag = qr_flag
        self.qr_operation = qr_operation
        self.qr_collisions = qr_collisions
        self.qr_threshold = qr_threshold
        self.md_flag = md_flag
        self.md_threshold = md_threshold

        np.random.seed(self.random_seed)
        random.seed(self.random_seed)
        torch.manual_seed(self.random_seed)
        torch.cuda.manual_seed(self.random_seed)

        """
            check cuda
        """
        if self.use_cuda and not torch.cuda.is_available():
            self.use_cuda = False
            print("Cuda is not available, automatically changed into cpu model")
        """
            check use fm, fwfm or ffm
        """
        if int(self.use_fm) + int(self.use_ffm) + int(self.use_fwfm) + int(self.use_logit) > 1:
            print("only support one type only, please make sure to choose only LR, FM, FFM or FwFM part")
            exit(1)
        elif self.use_logit:
            print("The model is logistic regression.")
        elif self.use_fm and self.use_deep:
            print("The model is deepfm(fm+deep layers)")
        elif self.use_ffm and self.use_deep:
            print("The model is deepffm(ffm+deep layers)")
        elif self.use_fwfm and self.use_deep:
            print("The model is deepfwfm(fwfm+deep layers)")
        elif self.use_fm:
            print("The model is fm only")
        elif self.use_ffm:
            print("The model is ffm only")
        elif self.use_fwfm:
            print("The model is fwfm only")
        elif self.use_deep:
            print("The model is deep layers only")
        else:
            print("You have to choose more than one of (fm, ffm, fwfm, deep) models to use")
            exit(1)

        """
            bias
        """
        if self.use_logit or self.use_fm or self.use_ffm or self.use_fwfm:
            self.bias = torch.nn.Parameter(torch.Tensor([0.01]))
        """
            LR/fm/fwfm part
        """
        if self.use_logit or self.use_fm or self.use_fwfm:
            if self.use_logit and self.verbose:
                print("Init Logistic regression")
            elif self.use_fm and self.verbose:
                print("Init fm part")
            elif self.verbose:
                print("Init fwfm part")
            if not self.use_fwlw:
                # no 1 dim emb bag for md possible
                if self.md_flag or not self.embedding_bag:
                    self.fm_1st_embeddings = nn.ModuleList(
                        [nn.Embedding(feature_size, 1) for feature_size in self.feature_sizes])
                else:
                    self.fm_1st_embeddings = self.create_emb(1, np.array(self.feature_sizes), sparse=False)
            if self.dropout_shallow:
                self.fm_first_order_dropout = nn.Dropout(self.dropout_shallow[0])
            if self.use_fm or self.use_fwfm:
                if self.embedding_bag:
                    self.fm_2nd_embeddings = self.create_emb(self.embedding_size, np.array(self.feature_sizes), sparse=False)
                else:
                    self.fm_2nd_embeddings = nn.ModuleList(
                        [nn.Embedding(feature_size, self.embedding_size) for feature_size in self.feature_sizes])
                if self.dropout_shallow:
                    self.fm_second_order_dropout = nn.Dropout(self.dropout_shallow[1])

                if (self.use_fm or self.use_fwfm or self.use_ffm) and self.use_lw:
                    self.fm_1st = nn.Linear(self.field_size, 1, bias=False)

                if (self.use_fm or self.use_fwfm or self.use_ffm) and self.use_fwlw:
                    self.fwfm_linear = nn.Linear(self.embedding_size, self.field_size, bias=False)

                if self.use_fwfm:
                    self.field_cov = nn.Linear(field_size, field_size, bias=False)

        """
            ffm part
        """
        if self.use_ffm:  # TODO if used embedding_bag
            print("Init ffm part")
            self.ffm_1st_embeddings = nn.ModuleList(
                [nn.Embedding(feature_size, 1) for feature_size in self.feature_sizes])
            if self.dropout_shallow:
                self.ffm_first_order_dropout = nn.Dropout(self.dropout_shallow[0])
            self.ffm_2nd_embeddings = nn.ModuleList(
                [nn.ModuleList([nn.Embedding(feature_size, self.embedding_size) for i in range(self.field_size)]) \
                 for feature_size in self.feature_sizes])
            if self.dropout_shallow:
                self.ffm_second_order_dropout = nn.Dropout(self.dropout_shallow[1])

        """
            deep parts
        """
        if self.use_deep:
            if self.verbose:
                print("Init deep part")
            for nidx in range(1, self.num_deeps + 1):
                if not self.use_fm and not self.use_ffm:
                    if self.embedding_bag:
                        self.fm_2nd_embeddings = self.create_emb(self.embedding_size, np.array(self.feature_sizes),
                                                                 sparse=False)
                    else:
                        self.fm_2nd_embeddings = nn.ModuleList(
                            [nn.Embedding(feature_size, self.embedding_size) for feature_size in self.feature_sizes])
                if self.is_deep_dropout:
                    setattr(self, 'net_' + str(nidx) + '_linear_0_dropout', nn.Dropout(self.dropout_deep[0]))

                setattr(self, 'net_' + str(nidx) + '_linear_1',
                        nn.Linear(self.field_size * self.embedding_size, self.deep_layers[0]))
                if self.is_batch_norm:
                    setattr(self, 'net_' + str(nidx) + '_batch_norm_1',
                            nn.BatchNorm1d(self.deep_layers[0], momentum=0.005))
                if self.is_deep_dropout:
                    setattr(self, 'net_' + str(nidx) + '_linear_1_dropout', nn.Dropout(self.dropout_deep[1]))

                for i, h in enumerate(self.deep_layers[1:], 1):
                    setattr(self, 'net_' + str(nidx) + '_linear_' + str(i + 1),
                            nn.Linear(self.deep_layers[i - 1], self.deep_layers[i]))
                    if self.is_batch_norm:
                        setattr(self, 'net_' + str(nidx) + '_batch_norm_' + str(i + 1),
                                nn.BatchNorm1d(self.deep_layers[i], momentum=0.005))
                    if self.is_deep_dropout:
                        setattr(self, 'net_' + str(nidx) + '_linear_' + str(i + 1) + '_dropout',
                                nn.Dropout(self.dropout_deep[i + 1]))
                setattr(self, 'net_' + str(nidx) + '_fc', nn.Linear(self.deep_layers[-1], 1, bias=False))

            """
                quantization part
            """
            if (self.static_quantization or self.quantization_aware) and self.use_deep:
                self.quant = QuantStub()
                self.dequant = DeQuantStub()

    def forward(self, Xi, Xv):
        """
        :param Xi_train: index input tensor, batch_size * embedding_size * 1
        :return: the last output
        """

        """
            fm/fwfm part
        """
        t00 = time()
        if self.use_logit or self.use_fm or self.use_fwfm:
            # dim: embedding_size * batch * 1, time cost 47%
            Tzero = torch.zeros(Xi.shape[0], 1, dtype=torch.long)
            if self.use_cuda:
                Tzero = Tzero.cuda()
            if not self.use_fwlw:
                if self.md_flag or not self.embedding_bag:
                    fm_1st_emb_arr = [(torch.sum(emb(Tzero), 1).t() * Xv[:, i]).t() if i < self.num else torch.sum(emb(Xi[:, i - self.num, :]), 1) for i, emb in enumerate(self.fm_1st_embeddings)]
                else:
                    fm_1st_emb_arr = [(emb(Tzero).t() * Xv[:, i]).t() if i < self.num else emb(Xi[:, i - self.num, :]) for i, emb in enumerate(self.fm_1st_embeddings)]

                # dim: batch_size * field_size
                fm_first_order = torch.cat(fm_1st_emb_arr, 1)
                if self.is_shallow_dropout:
                    fm_first_order = self.fm_first_order_dropout(fm_first_order)
                # print(fm_first_order.shape, "old linear")
            # dim: field_size * batch_size * embedding_size, time cost 43%
            if self.use_fm or self.use_fwfm:
                if self.embedding_bag:
                    fm_2nd_emb_arr = [(emb(Tzero).t() * Xv[:, i]).t() if i < self.num else emb(Xi[:, i - self.num, :]) for i, emb in enumerate(self.fm_2nd_embeddings)]
                else:
                    fm_2nd_emb_arr = [(torch.sum(emb(Tzero), 1).t() * Xv[:, i]).t() if i < self.num else torch.sum(emb(Xi[:, i - self.num, :]), 1) for i, emb in enumerate(self.fm_2nd_embeddings)]
                # convert a list of tensors to tensor
                fm_second_order_tensor = torch.stack(fm_2nd_emb_arr)
                if self.use_fwlw:
                    if self.dynamic_quantization or (self.static_quantization and not self.static_calibrate) or (self.quantization_aware and not self.use_cuda):
                        fwfm_linear = torch.einsum('ijk,ik->ijk', [fm_second_order_tensor, self.fwfm_linear.weight().dequantize()])
                    else:
                        fwfm_linear = torch.einsum('ijk,ik->ijk', [fm_second_order_tensor, self.fwfm_linear.weight])
                    fm_first_order = torch.einsum('ijk->ji', [fwfm_linear])
                    if self.is_shallow_dropout:
                        fm_first_order = self.fm_first_order_dropout(fm_first_order)
                    # print(fm_first_order.shape, "new fwfm linear")

                # compute outer product, outer_fm: 39x39x2048x10
                outer_fm = torch.einsum('kij,lij->klij', fm_second_order_tensor, fm_second_order_tensor)
                if self.use_fm:
                    fm_second_order = (torch.sum(torch.sum(outer_fm, 0), 0) - torch.sum(
                        torch.einsum('kkij->kij', outer_fm), 0)) * 0.5
                else:
                    # dequantize since einsum is not supported for quantization
                    if self.dynamic_quantization or (self.static_quantization and not self.static_calibrate) or (self.quantization_aware and not self.use_cuda):
                        '''q_func = QFunctional()
                        q_add = q_func.add(self.field_cov.weight().t(), self.field_cov.weight()) # without transpose better?
                        q_add_mul = q_func.mul_scalar(q_add, 0.5)
                        outer_fwfm = torch.einsum('klij,kl->klij', outer_fm, torch.dequantize(q_add_mul))'''
                        ''''# better for loss:
                        if hasattr(self.field_cov, '_packed_params') and self.field_cov._packed_params.dtype == torch.float16:
                            outer_fwfm = torch.einsum('klij,kl->klij', outer_fm, (self.field_cov.weight().t() + self.field_cov.weight()) * 0.5)
                        else:'''
                        outer_fwfm = torch.einsum('klij,kl->klij', outer_fm, (self.field_cov.weight().dequantize().t() + self.field_cov.weight().dequantize()) * 0.5)

                    # time cost 3%
                    else:
                        outer_fwfm = torch.einsum('klij,kl->klij', outer_fm,
                                              (self.field_cov.weight.t() + self.field_cov.weight) * 0.5)

                    fm_second_order = (torch.sum(torch.sum(outer_fwfm, 0), 0) - torch.sum(
                        torch.einsum('kkij->kij', outer_fwfm), 0)) * 0.5
                if self.is_shallow_dropout:
                    fm_second_order = self.fm_second_order_dropout(fm_second_order)
                # print(fm_second_order.shape)
        """
            ffm part
        """
        if self.use_ffm:
            ffm_1st_emb_arr = [(torch.sum(emb(Tzero), 1).t() * Xv[:, i]).t() if i < self.num else torch.sum(
                emb(Xi[:, i - self.num, :]), 1) \
                               for i, emb in enumerate(self.ffm_1st_embeddings)]
            ffm_first_order = torch.cat(ffm_1st_emb_arr, 1)
            if self.is_shallow_dropout:
                ffm_first_order = self.ffm_first_order_dropout(ffm_first_order)
            ffm_2nd_emb_arr = [[(torch.sum(emb(Tzero), 1).t() * Xv[:, i]).t() if i < self.num else torch.sum(
                emb(Xi[:, i - self.num, :]), 1) \
                                for emb in f_embs] for i, f_embs in enumerate(self.ffm_2nd_embeddings)]
            ffm_wij_arr = []
            for i in range(self.field_size):
                for j in range(i + 1, self.field_size):
                    ffm_wij_arr.append(ffm_2nd_emb_arr[i][j] * ffm_2nd_emb_arr[j][i])
            ffm_second_order = sum(ffm_wij_arr)
            if self.is_shallow_dropout:
                ffm_second_order = self.ffm_second_order_dropout(ffm_second_order)

        """
            deep part
        """
        if self.use_deep:
            if self.use_fm or self.use_fwfm:
                deep_emb = torch.cat(fm_2nd_emb_arr, 1)
            elif self.use_ffm:
                deep_emb = torch.cat([sum(ffm_second_order_embs) for ffm_second_order_embs in ffm_2nd_emb_arr], 1)
            else:
                deep_emb = torch.cat([(torch.sum(emb(Xi[:, i, :]), 1).t() * Xv[:, i]).t() for i, emb in
                                      enumerate(self.fm_2nd_embeddings)], 1)
            if self.deep_layers_activation == 'sigmoid':
                activation = torch.sigmoid
            elif self.deep_layers_activation == 'tanh':
                activation = torch.tanh
            else:
                activation = torch.relu

            if self.static_quantization or self.quantization_aware:
                deep_emb = self.quant(deep_emb)
                #deep_emb = torch.quantize_per_tensor(deep_emb, 0.1, 100, torch.quint8)

            deep_embs = {}
            x_deeps = {}
            for nidx in range(1, self.num_deeps + 1):
                if self.is_deep_dropout:
                    deep_emb = getattr(self, 'net_' + str(nidx) + '_linear_0_dropout')(deep_emb)
                x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_linear_1')(deep_emb)
                if self.is_batch_norm:
                    x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_batch_norm_1')(x_deeps[nidx])
                x_deeps[nidx] = activation(x_deeps[nidx])
                if self.is_deep_dropout:
                    x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_linear_1_dropout')(x_deeps[nidx])

                for i in range(1, len(self.deep_layers)):
                    x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_linear_' + str(i + 1))(x_deeps[nidx])
                    if self.is_batch_norm:
                        x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_batch_norm_' + str(i + 1))(x_deeps[nidx])
                    x_deeps[nidx] = activation(x_deeps[nidx])
                    if self.is_deep_dropout:
                        x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_linear_' + str(i + 1) + '_dropout')(
                            x_deeps[nidx])

                x_deeps[nidx] = getattr(self, 'net_' + str(nidx) + '_fc')(x_deeps[nidx])

            x_deep = x_deeps[1]

            for nidx in range(2, self.num_deeps + 1):
                x_deep = x_deeps[nidx]

            if self.static_quantization or self.quantization_aware:
                x_deep = self.dequant(x_deep)

        """
            sum
        """
        # print(fm_first_order.shape, "linear dim")
        # print(torch.sum(fm_first_order,1).shape, "sum dim")

        # total_sum dim: batch, time cost 1.3%
        if (self.use_fm or self.use_fwfm) and self.use_lw:
            if self.dynamic_quantization or (self.static_quantization and not self.static_calibrate) or (
                    self.quantization_aware and not self.use_cuda):
                fm_first_order = torch.matmul(fm_first_order, self.fm_1st.weight().dequantize().t())
            else:
                fm_first_order = torch.matmul(fm_first_order, self.fm_1st.weight.t())

        elif self.use_ffm and self.lw:
            ffm_first_order = torch.matmul(ffm_first_order, self.ffm_1st.weight.t())

        if self.use_logit:
            total_sum = torch.sum(fm_first_order, 1) + self.bias
        elif (self.use_fm or self.use_fwfm) and self.use_deep:
            total_sum = torch.sum(fm_first_order, 1) + torch.sum(fm_second_order, 1) + torch.sum(x_deep, 1) + self.bias
        elif self.use_ffm and self.use_deep:
            total_sum = torch.sum(ffm_first_order, 1) + torch.sum(ffm_second_order, 1) + torch.sum(x_deep,
                                                                                                   1) + self.bias
        elif self.use_fm or self.use_fwfm:
            total_sum = torch.sum(fm_first_order, 1) + torch.sum(fm_second_order, 1) + self.bias
        elif self.use_ffm:
            total_sum = torch.sum(ffm_first_order, 1) + torch.sum(ffm_second_order, 1) + self.bias
        else:
            total_sum = torch.sum(x_deep, 1) + self.bias

        return total_sum

    # credit to https://github.com/ChenglongChen/tensorflow-DeepFM/blob/master/DeepFM.py
    def init_weights(self):
        model = self.train()
        require_update = True
        last_layer_size = 0
        TORCH = torch.cuda if self.use_cuda else torch
        for name, param in model.named_parameters():
            if '1st_embeddings' in name:
                param.data = TORCH.FloatTensor(param.data.size()).normal_()
            elif '2nd_embeddings' in name:
                param.data = TORCH.FloatTensor(param.data.size()).normal_().mul(0.01)
            elif 'linear' in name:
                if 'weight' in name:  # weight and bias in the same layer share the same glorot
                    glorot = np.sqrt(2.0 / np.sum(param.data.shape))
                param.data = TORCH.FloatTensor(param.data.size()).normal_().mul(glorot)
            elif 'field_cov.weight' == name:
                param.data = TORCH.FloatTensor(param.data.size()).normal_().mul(np.sqrt(2.0 / self.field_size / 2))
            else:
                if (self.use_fwfm or self.use_fm) and require_update:
                    last_layer_size += (self.field_size + self.embedding_size)
                if self.use_deep and require_update:
                    last_layer_size += (self.deep_layers[-1] + 1)
                require_update = False
                if name in ['fm_1st.weight', 'fm_2nd.weight'] or 'fc.weight' in name:
                    param.data = TORCH.FloatTensor(param.data.size()).normal_().mul(np.sqrt(2.0 / last_layer_size))

    def fit(self, Xi_train, Xv_train, y_train, Xi_valid=None, Xv_valid=None,
            y_valid=None, early_stopping=False, refit=False, save_path=None, prune=0, prune_fm=0, prune_r=0,
            prune_deep=0, emb_r=1., emb_corr=1., quantization_aware=False, teacher_model=None):
        """
        :param Xi_train: [[ind1_1, ind1_2, ...], [ind2_1, ind2_2, ...], ..., [indi_1, indi_2, ..., indi_j, ...], ...]
                        indi_j is the feature index of feature field j of sample i in the training set
        :param Xv_train: [[val1_1, val1_2, ...], [val2_1, val2_2, ...], ..., [vali_1, vali_2, ..., vali_j, ...], ...]
                        vali_j is the feature value of feature field j of sample i in the training set
                        vali_j can be either binary (1/0, for binary/categorical features) or float (e.g., 10.24, for numerical features)
        :param y_train: label of each sample in the training set
        :param Xi_valid: list of list of feature indices of each sample in the validation set
        :param y_valid: label of each sample in the validation set
        :param early_stopping: perform early stopping or not
        :param refit: refit the model on the train+valid dataset or not
        :param save_path: the path to save the model
        :param prune: control module to decide if to prune or not
        :param prune_fm: if prune the FM component
        :param prune_deep: if prune the DEEP component
        :param emb_r: ratio of sparse rate in FM over sparse rate in Deep
        :return:
        """
        """
        pre_process
        """

        '''
        if save_path and not os.path.exists('/'.join(save_path.split('/')[0:-1])):
            print("Save path is not existed!")
            return
        '''

        if self.verbose:
            print("pre_process data ing...")
        is_valid = False
        Xi_train = np.array(Xi_train).reshape((-1, self.field_size - self.num, 1))
        Xv_train = np.array(Xv_train)
        y_train = np.array(y_train)
        x_size = Xi_train.shape[0]
        if Xi_valid:
            Xi_valid = np.array(Xi_valid).reshape((-1, self.field_size - self.num, 1))
            Xv_valid = np.array(Xv_valid)
            y_valid = np.array(y_valid)
            x_valid_size = Xi_valid.shape[0]
            is_valid = True
        if self.verbose:
            print("pre_process data finished")

        print('init_weights')
        self.init_weights()

        """
            train model
        """
        model = self.train()

        optimizer = torch.optim.SGD(self.parameters(), lr=self.learning_rate, momentum=self.momentum,
                                    weight_decay=self.weight_decay)
        if self.optimizer_type == 'adam':
            optimizer = torch.optim.Adam(self.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.optimizer_type == 'rmsp':
            optimizer = torch.optim.RMSprop(self.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)
        elif self.optimizer_type == 'adag':
            optimizer = torch.optim.Adagrad(self.parameters(), lr=self.learning_rate, weight_decay=self.weight_decay)
        criterion = F.binary_cross_entropy_with_logits

        train_result = []
        valid_result = []
        num_total = 0
        num_1st_order_embeddings = 0
        num_2nd_order_embeddings = 0
        num_dnn = 0
        print('========')
        for name, param in model.named_parameters():
            if self.verbose:
                print(name, param.data.shape)
            num_total += np.prod(param.data.shape)
            if '1st_embeddings' in name:
                num_1st_order_embeddings += np.prod(param.data.shape)
            if '2nd_embeddings' in name:
                num_2nd_order_embeddings += np.prod(param.data.shape)
            if 'linear_' in name:
                num_dnn += np.prod(param.data.shape)
            if 'field_cov.weight' == name:
                symm_sum = 0.5 * (param.data + param.data.t())
                non_zero_r = (symm_sum != 0).sum().item()
        print('Summation of feature sizes: %s' % (sum(self.feature_sizes)))
        print('Number of 1st order embeddings: %d' % (num_1st_order_embeddings))
        print('Number of 2nd order embeddings: %d' % (num_2nd_order_embeddings))
        print('Number of 2nd order interactions: %d' % (non_zero_r))
        print('Number of DNN parameters: %d' % (num_dnn))
        print("Number of total parameters: %d" % (num_total))
        print('========')
        num_total_original = num_total

        n_iter = 0
        for epoch in range(self.n_epochs):
            total_loss = 0.0
            batch_iter = x_size // self.batch_size
            epoch_begin_time = time()
            batch_begin_time = time()

            """
                teacher model
            """
            if teacher_model:
                # fetch teacher outputs using teacher_model under eval() mode
                loading_start = time()
                teacher_model.eval()
                teacher_outputs = self.fetch_teacher_outputs(teacher_model, Xi_train, Xv_train, x_size)
                elapsed_time = math.ceil(time() - loading_start)
                logging.info("- Finished computing teacher outputs after {} secs..".format(elapsed_time))

            for i in range(batch_iter + 1):
                if epoch >= self.warm:
                    n_iter += 1
                offset = i * self.batch_size
                end = min(x_size, offset + self.batch_size)
                if offset == end:
                    break
                batch_xi = Variable(torch.LongTensor(Xi_train[offset:end]))
                batch_xv = Variable(torch.FloatTensor(Xv_train[offset:end]))
                batch_y = Variable(torch.FloatTensor(y_train[offset:end]))
                if self.use_cuda:
                    batch_xi, batch_xv, batch_y = batch_xi.cuda(), batch_xv.cuda(), batch_y.cuda()
                optimizer.zero_grad()
                outputs = model(batch_xi, batch_xv)
                if teacher_model:
                    output_teacher_batch = torch.from_numpy(teacher_outputs[i])
                    if self.use_cuda:
                        output_teacher_batch = output_teacher_batch.cuda()
                    output_teacher_batch = Variable(output_teacher_batch, requires_grad=False)

                    loss = self.loss_fn_kd(outputs, output_teacher_batch, batch_y)
                else:
                    loss = criterion(outputs, batch_y)

                loss.backward()
                optimizer.step()

                total_loss += loss.data.item()
                if self.verbose and i % 100 == 99:
                    eval = self.evaluate(batch_xi, batch_xv, batch_y)
                    print('[%d, %5d] loss: %.6f metric: %.6f time: %.1f s' %
                          (epoch + 1, i + 1, total_loss / 100.0, eval, time() - batch_begin_time))
                    total_loss = 0.0
                    batch_begin_time = time()

                if prune and (i == batch_iter or i % 10 == 9) and epoch >= self.warm:
                    # adaptive increases faster in the early phase when the network is stable and slower in the late phase when the network becomes sensitive
                    self.adaptive_sparse = self.target_sparse * (1 - 0.99 ** (n_iter / 100.))
                    if prune_fm != 0:
                        stacked_embeddings = []
                        for name, param in model.named_parameters():
                            if 'fm_2nd_embeddings' in name:
                                stacked_embeddings.append(param.data)
                        stacked_emb = torch.cat(stacked_embeddings, 0)
                        emb_threshold = self.binary_search_threshold(stacked_emb.data, self.adaptive_sparse * emb_r,
                                                                     np.prod(stacked_emb.data.shape))
                    for name, param in model.named_parameters():
                        if 'fm_2nd_embeddings' in name and prune_fm != 0:
                            mask = abs(param.data) < emb_threshold
                            param.data[mask] = 0
                        if 'linear' in name and 'weight' in name and prune_deep != 0:
                            layer_pars = np.prod(param.data.shape)
                            threshold = self.binary_search_threshold(param.data, self.adaptive_sparse, layer_pars)
                            mask = abs(param.data) < threshold
                            param.data[mask] = 0
                        if 'field_cov.weight' == name and prune_r != 0:
                            layer_pars = np.prod(param.data.shape)
                            symm_sum = 0.5 * (param.data + param.data.t())
                            threshold = self.binary_search_threshold(symm_sum, self.adaptive_sparse * emb_corr,
                                                                     layer_pars)
                            mask = abs(symm_sum) < threshold
                            param.data[mask] = 0
                            # print (mask.sum().item(), layer_pars)

            # epoch evaluation metrics
            no_non_sparse = 0
            for name, param in model.named_parameters():
                no_non_sparse += (param != 0).sum().item()
            print('Model parameters %d, sparse rate %.2f%%' % (no_non_sparse, 100 - no_non_sparse * 100. / num_total))
            train_loss, train_eval, train_prauc, train_rce = self.eval_by_batch(Xi_train, Xv_train, y_train, x_size)
            train_result.append(train_eval)
            print('Training [%d] loss: %.6f metric: %.6f prauc: %.4f rce: %.2f sparse %.2f%% time: %.1f s' %
                  (
                      epoch + 1, train_loss, train_eval, train_prauc, train_rce, 100 - no_non_sparse * 100. / num_total,
                      time() - epoch_begin_time))
            if is_valid:
                valid_loss, valid_eval, vaild_prauc, valid_rce = self.eval_by_batch(Xi_valid, Xv_valid, y_valid,
                                                                                    x_valid_size)
                valid_result.append(valid_eval)
                print('Validation [%d] loss: %.6f metric: %.6f prauc: %.4f rce: %.2f sparse %.2f%% time: %.1f s' %
                      (
                          epoch + 1, valid_loss, valid_eval, vaild_prauc, valid_rce,
                          100 - no_non_sparse * 100. / num_total,
                          time() - epoch_begin_time))
            print('*' * 50)

            # shuffle training dataset
            permute_idx = np.random.permutation(x_size)
            Xi_train = Xi_train[permute_idx]
            Xv_train = Xv_train[permute_idx]
            y_train = y_train[permute_idx]
            if self.verbose:
                print('Training dataset shuffled.')

            if save_path:
                torch.save(self.state_dict(), save_path)
            if is_valid and early_stopping and self.training_termination(valid_result):
                print("early stop at [%d] epoch!" % (epoch + 1))
                break

            # quantization aware training
            if self.quantization_aware:
                self.cuda()
                self.use_cuda = True
                if epoch > self.n_epochs - 2: # TODO right number of epochs
                    # Freeze quantizer parameters
                    self.apply(torch.quantization.disable_observer)
                if epoch > self.n_epochs - 1:
                    # Freeze batch norm mean and variance estimates
                    self.apply(torch.nn.intrinsic.qat.freeze_bn_stats)

        if prune: # summary
            num_total = 0
            num_1st_order_embeddings = 0
            num_2nd_order_embeddings = 0
            num_dnn = 0
            non_zero_r = 0
            print('========')
            for name, param in model.named_parameters():
                num_total += (param != 0).sum().item()
                if '1st_embeddings' in name:
                    num_1st_order_embeddings += (param != 0).sum().item()
                if '2nd_embeddings' in name:
                    num_2nd_order_embeddings += (param != 0).sum().item()
                if 'linear_' in name:
                    num_dnn += (param != 0).sum().item()
                if 'field_cov.weight' == name:
                    symm_sum = 0.5 * (param.data + param.data.t())
                    non_zero_r = (symm_sum != 0).sum().item()
            print('Number of pruned 1st order embeddings: %d' % (num_1st_order_embeddings))
            print('Number of pruned 2nd order embeddings: %d' % (num_2nd_order_embeddings))
            print('Number of pruned 2nd order interactions: %d' % (non_zero_r))
            print('Number of pruned DNN parameters: %d' % (num_dnn))
            print("Number of pruned total parameters: %d" % (num_total))
            print("Non pruned model parameters: \t%d" % (num_total_original))
            print("Difference: \t%d" % (num_total_original-num_total))

    def eval_by_batch(self, Xi, Xv, y, x_size):
        if self.quantization_aware:
            model = self.to('cpu')
            model.use_cuda = False
            model = torch.quantization.convert(model.eval(), inplace=False)
            model.eval()
        else:
            model = self.eval()
        total_loss = 0.0
        y_pred = []
        if self.use_ffm:
            batch_size = 8192 * 2
        else:
            batch_size = 8192
        batch_iter = x_size // batch_size
        criterion = F.binary_cross_entropy_with_logits
        for i in range(batch_iter + 1):
            offset = i * batch_size
            end = min(x_size, offset + batch_size)
            if offset == end:
                break
            batch_xi = Variable(torch.LongTensor(Xi[offset:end]))
            batch_xv = Variable(torch.FloatTensor(Xv[offset:end]))
            batch_y = Variable(torch.FloatTensor(y[offset:end]))
            if self.use_cuda:
                batch_xi, batch_xv, batch_y = batch_xi.cuda(), batch_xv.cuda(), batch_y.cuda()
            outputs = model(batch_xi, batch_xv)
            pred = torch.sigmoid(outputs).cpu()
            y_pred.extend(pred.data.numpy().astype("float64"))
            loss = criterion(outputs, batch_y)
            total_loss += loss.data.item() * (end - offset)
        total_metric = self.eval_metric(y, y_pred)
        prauc = self.compute_prauc(y_pred, y)
        rce = self.compute_rce(y_pred, y)
        return total_loss / x_size, total_metric, prauc, rce

    def compute_prauc(self, pred, gt):
        prec, recall, thresh = precision_recall_curve(gt, pred)
        prauc = auc(recall, prec)
        return prauc

    def calculate_ctr(self, gt):
        positive = len([x for x in gt if x == 1])
        ctr = positive / float(len(gt))
        return ctr

    def compute_rce(self, pred, gt):
        cross_entropy = log_loss(gt, pred)
        data_ctr = self.calculate_ctr(gt)
        strawman_cross_entropy = log_loss(gt, [data_ctr for _ in range(len(gt))])
        return (1.0 - cross_entropy / strawman_cross_entropy) * 100.0

    def cross_entropy(self, predictions, targets):
        N = predictions.shape[0]
        ce = -np.sum(targets * np.log(predictions)) / N
        return ce

    def binary_search_threshold(self, param, target_percent, total_no):
        l, r = 0., 1e2
        cnt = 0
        while l < r:
            cnt += 1
            mid = (l + r) / 2
            sparse_items = (abs(param) < mid).sum().item() * 1.0
            sparse_rate = sparse_items / total_no
            if abs(sparse_rate - target_percent) < 0.0001:
                return mid
            elif sparse_rate > target_percent:
                r = mid
            else:
                l = mid
            if cnt > 100:
                break
        return mid

    # shuffle three lists simutaneously
    def shuffle_in_unison_scary(self, a, b, c):
        rng_state = np.random.get_state()
        np.random.shuffle(a)
        np.random.set_state(rng_state)
        np.random.shuffle(b)
        np.random.set_state(rng_state)
        np.random.shuffle(c)

    def training_termination(self, valid_result):
        if len(valid_result) > 4:
            if self.greater_is_better:
                if valid_result[-1] < valid_result[-2] and \
                        valid_result[-2] < valid_result[-3] and \
                        valid_result[-3] < valid_result[-4]:
                    return True
            else:
                if valid_result[-1] > valid_result[-2] and \
                        valid_result[-2] > valid_result[-3] and \
                        valid_result[-3] > valid_result[-4]:
                    return True
        return False

    def predict(self, Xi, Xv):
        """
        :param Xi: the same as fit function
        :param Xv: the same as fit function
        :return: output, ont-dim array
        """
        Xi = np.array(Xi).reshape((-1, self.field_size, 1))
        Xi = Variable(torch.LongTensor(Xi))
        Xv = Variable(torch.FloatTensor(Xv))
        if self.use_cuda and torch.cuda.is_available():
            Xi, Xv = Xi.cuda(), Xv.cuda()

        model = self.eval()
        pred = torch.sigmoid(model(Xi, Xv)).cpu()
        return (pred.data.numpy() > 0.5)

    def predict_proba(self, Xi, Xv):
        Xi = np.array(Xi).reshape((-1, self.field_size, 1))
        Xi = Variable(torch.LongTensor(Xi))
        Xv = Variable(torch.FloatTensor(Xv))
        if self.use_cuda and torch.cuda.is_available():
            Xi, Xv = Xi.cuda(), Xv.cuda()

        model = self.eval()
        pred = torch.sigmoid(model(Xi, Xv)).cpu()
        return pred.data.numpy()

    def inner_predict(self, Xi, Xv):
        """
        :param Xi: tensor of feature index
        :param Xv: tensor of feature value
        :return: output, numpy
        """
        model = self.eval()
        pred = torch.sigmoid(model(Xi, Xv)).cpu()
        return (pred.data.numpy() > 0.5)

    def inner_predict_proba(self, Xi, Xv):
        """
        :param Xi: tensor of feature index
        :param Xv: tensor of feature value
        :return: output, numpy
        """
        model = self.eval()
        pred = torch.sigmoid(model(Xi, Xv)).cpu()
        return pred.data.numpy()

    def evaluate(self, Xi, Xv, y):
        """
        :param Xi: tensor of feature index
        :param Xv: tensor of feature value
        :param y: tensor of labels
        :return: metric of the evaluation
        """
        y_pred = self.inner_predict_proba(Xi, Xv)
        return self.eval_metric(y.cpu().data.numpy(), y_pred)

    def print_size_of_model(self):
        torch.save(self.state_dict(), "temp.p")
        size = os.path.getsize("temp.p")
        print('\tSize (MB):', size / 1e6)
        os.remove('temp.p')
        return size

    def time_model_evaluation(self, Xi, Xv, y, cuda=False, quantization_aware=False):
        torch.set_num_threads(1)

        Xi = np.array(Xi).reshape((-1, self.field_size - self.num, 1))
        Xv = np.array(Xv)
        y = np.array(y)
        x_size = Xi.shape[0]

        #with torch.autograd.profiler.profile() as prof:
            #loss, total_metric, prauc, rce = self.eval_by_batch(Xi, Xv, y, x_size)
        #print(prof.key_averages().table(sort_by="self_cpu_time_total"))
        s = time()
        loss, total_metric, prauc, rce = self.eval_by_batch(Xi, Xv, y, x_size)
        elapsed = time() - s

        print('''\tLoss: {0:.3f}\n\tAcc: {1:.3f}\n\tElapsed time (seconds): {2:.3f}'''.format(loss, total_metric, elapsed))

        Xi_1 = torch.LongTensor([Xi[0]])
        Xv_1 = torch.FloatTensor([Xv[0]])
        if cuda:
            Xi_1 = Xi_1.cuda()
            Xv_1 = Xv_1.cuda()

        if self.quantization_aware:
            model = self.to('cpu')
            model.use_cuda = False
            model = torch.quantization.convert(model.eval(), inplace=False)
            model.eval()
        else:
            model = self.eval()

        i = 0
        time_spent = []
        while i < 500:
            start_time = time()
            with torch.no_grad():
                _ = model(Xi_1, Xv_1)
            if cuda:
                torch.cuda.synchronize()  # wait for cuda to finish (cuda is asynchronous!)
            if i != 0:
                time_spent.append(time() - start_time)
            i += 1
        print('\tAvg execution time per forward(ms): {:.5f}'.format(np.mean(time_spent)))

    def fetch_teacher_outputs(self, teacher_model, Xi, Xv, x_size):
        teacher_model.eval()
        teacher_outputs = []
        batch_size = self.batch_size
        batch_iter = x_size // batch_size
        for i in range(batch_iter + 1):
            offset = i * batch_size
            end = min(x_size, offset + batch_size)
            if offset == end:
                break
            batch_xi = Variable(torch.LongTensor(Xi[offset:end]))
            batch_xv = Variable(torch.FloatTensor(Xv[offset:end]))
            if self.use_cuda:
                batch_xi, batch_xv = batch_xi.cuda(), batch_xv.cuda()

            output_teacher_batch = teacher_model(batch_xi, batch_xv).data.cpu().numpy()
            teacher_outputs.append(output_teacher_batch)

        return teacher_outputs

    def loss_fn_kd(self, outputs, teacher_outputs, y):
        """
        Compute the knowledge-distillation (KD) loss given outputs, labels.
        "Hyperparameters": temperature and alpha

        NOTE: the KL Divergence for PyTorch comparing the softmaxs of teacher
        and student expects the input tensor to be log probabilities! See Issue https://github.com/peterliht/knowledge-distillation-pytorch/issues/2
        """
        alpha = 0.9 #params.alpha
        T = 20 #params.temperature
        KD_loss = nn.KLDivLoss()(F.log_softmax(outputs / T, dim=0),
                                 F.softmax(teacher_outputs / T, dim=0)) * (alpha * T * T) + \
                  F.binary_cross_entropy_with_logits(outputs, y) * (1. - alpha)

        return KD_loss

    def create_emb(self, m, ln, sparse=True):
        emb_l = nn.ModuleList()
        for i in range(0, ln.size):
            n = ln[i]
            # construct embedding operator
            if self.qr_flag and n > self.qr_threshold:
                EE = QREmbeddingBag(n, m, self.qr_collisions,
                                    operation=self.qr_operation, mode="sum", sparse=sparse)
            elif self.md_flag:
                base = max(m)
                _m = m[i] if n > self.md_threshold else base
                EE = PrEmbeddingBag(n, _m, base)
                # use np initialization as below for consistency...
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, _m)
                ).astype(np.float32)
                EE.embs.weight.data = torch.tensor(W, requires_grad=True)

            else:
                EE = nn.EmbeddingBag(n, m, mode="sum", sparse=sparse)

                # initialize embeddings
                # nn.init.uniform_(EE.weight, a=-np.sqrt(1 / n), b=np.sqrt(1 / n))
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, m)
                ).astype(np.float32)
                # approach 1
                EE.weight.data = torch.tensor(W, requires_grad=True)
                # approach 2
                # EE.weight.data.copy_(torch.tensor(W))
                # approach 3
                # EE.weight = Parameter(torch.tensor(W),requires_grad=True)

            emb_l.append(EE)

        return emb_l

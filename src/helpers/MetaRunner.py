# -*- coding: UTF-8 -*-

import os
import gc
import copy
import torch
import logging
import numpy as np
import pandas as pd
import random

from time import time
from tqdm import tqdm
from torch.utils.data import DataLoader
from typing import Dict, List, NoReturn
from collections import defaultdict

from utils import utils
from models.MetaModel import MetaModel

import matplotlib.pyplot as plt
import re


class MetaRunner(object):
    @staticmethod
    def parse_runner_args(parser):
        parser.add_argument('--epoch', type=int, default=100,
                            help='Number of epochs.')
        parser.add_argument('--tepoch', type=int, default=10,
                            help='Number of epochs.')
        parser.add_argument('--check_epoch', type=int, default=1,
                            help='Check some tensors every check_epoch.')
        parser.add_argument('--early_stop', type=int, default=5,
                            help='The number of epochs when dev results drop continuously.')
        parser.add_argument('--lr', type=float, default=0.001,
                            help='Learning rate.')
        parser.add_argument('--l2', type=float, default=1e-04,
                            help='Weight decay in optimizer.')
        parser.add_argument('--batch_size', type=int, default=256,
                            help='Batch size during training.')
        parser.add_argument('--eval_batch_size', type=int, default=256,
                            help='Batch size during testing.')
        parser.add_argument('--optimizer', type=str, default='Adam',
                            help='optimizer: GD, Adam, Adagrad, Adadelta')
        parser.add_argument('--num_workers', type=int, default=4,
                            help='Number of processors when prepare batches in DataLoader')
        parser.add_argument('--pin_memory', type=int, default=1,
                            help='pin_memory in DataLoader')
        parser.add_argument('--topk', type=str, default='[5]',
                            help='The number of items recommended to each user.')
        parser.add_argument('--metric', type=str, default='["NDCG"]',
                            help='metrics: NDCG, HR')

        return parser

    @staticmethod
    def evaluate_method(predictions: np.ndarray, topk: list, metrics: list) -> Dict[str, float]:
        """
        :param predictions: (-1, n_candidates) shape, the first column is the score for ground-truth item
        :param topk: top-K values list
        :param metrics: metrics string list
        :return: a result dict, the keys are metrics@topk
        """
        evaluations = dict()
        sort_idx = (-predictions).argsort(axis=1)
        gt_rank = np.argwhere(sort_idx == 0)[:, 1] + 1
        for k in topk:
            hit = (gt_rank <= k)
            for metric in metrics:
                key = '{}@{}'.format(metric, k)
                if metric == 'HR':
                    evaluations[key] = hit.mean(dtype=np.float16)
                elif metric == 'NDCG':
                    evaluations[key] = (hit / np.log2(gt_rank + 1)).mean(dtype=np.float16)
                else:
                    raise ValueError('Undefined evaluation metric: {}.'.format(metric))
        return evaluations

    def __init__(self, args, corpus):
        self.epoch = args.epoch
        self.check_epoch = args.check_epoch
        self.early_stop = args.early_stop
        self.learning_rate = args.lr
        self.batch_size = args.batch_size
        self.eval_batch_size = args.eval_batch_size
        self.keys = args.keys
        self.l2 = args.l2
        self.optimizer_name = args.optimizer
        self.num_workers = args.num_workers
        self.pin_memory = args.pin_memory
        self.topk = eval(args.topk)
        self.metrics = [m.strip().upper() for m in eval(args.metric)]
        self.result_file = args.result_file
        self.dyn_method = args.dyn_method
        # self.dyn_update = args.dyn_update
        #self.meta_name = args.meta_name
        self.main_metric = '{}@{}'.format(self.metrics[0], self.topk[0])  # early stop based on main_metric

        self.time = None  # will store [start_time, last_step_time]


        self.snap_boundaries = corpus.snap_boundaries
        self.snapshots_path = corpus.snapshots_path
        self.test_result_file = args.test_result_file
        self.tepoch = args.tepoch
        self.DRM = args.DRM

        self.tester = Tester(args, corpus)



    def _check_time(self, start=False):
        if self.time is None or start:
            self.time = [time()] * 2
            return self.time[0]
        tmp_time = self.time[1]
        self.time[1] = time()
        return self.time[1] - tmp_time

    def _build_optimizer(self, model):
        optimizer_name = self.optimizer_name.lower()
        if optimizer_name == 'gd':
            #logging.info("Optimizer: GD")
            optimizer = torch.optim.SGD(
                model.customize_parameters(), lr=self.learning_rate, weight_decay=self.l2) # betas=(0.5, 0.99), amsgrad=True
        elif optimizer_name == 'adagrad':
            #logging.info("Optimizer: Adagrad")
            optimizer = torch.optim.Adagrad(
                model.customize_parameters(), lr=self.learning_rate, weight_decay=self.l2)
        elif optimizer_name == 'adadelta':
            #logging.info("Optimizer: Adadelta")
            optimizer = torch.optim.Adadelta(
                model.customize_parameters(), lr=self.learning_rate, weight_decay=self.l2)
        elif optimizer_name == 'adam':
            #logging.info("Optimizer: Adam")
            if 'parameters' in self.DRM:
                optimizer = torch.optim.Adam(model.parameters(), lr=self.learning_rate, weight_decay=self.l2)
            else:
                optimizer = torch.optim.Adam(
                model.customize_parameters(), lr=self.learning_rate, weight_decay=self.l2)
        else:
            raise ValueError("Unknown Optimizer: " + self.optimizer_name)
        return optimizer

    def eval_termination(self, criterion: List[float]) -> bool:
        if len(criterion) > 20 and utils.non_increasing(criterion[-self.early_stop:]):
            return True
        elif len(criterion) - criterion.index(max(criterion)) > 20:
            return True
        return False

    def evaluate(self, model: torch.nn.Module, data: MetaModel.Dataset, topks: list, metrics: list) -> Dict[str, float]:
        """
        Evaluate the results for an input dataset.
        :return: result dict (key: metric@k)
        """
        predictions = self.predict(model, data)
        return self.evaluate_method(predictions, topks, self.metrics)

    def predict(self, model: torch.nn.Module, data: MetaModel.Dataset) -> np.ndarray:
        """
        The returned prediction is a 2D-array, each row corresponds to all the candidates,
        and the ground-truth item poses the first.
        Example: ground-truth items: [1, 2], 2 negative items for each instance: [[3,4], [5,6]]
                 predictions order: [[1,3,4], [2,5,6]]
        """
        model.eval()
        predictions = list()
        dl = DataLoader(data, batch_size=self.eval_batch_size, shuffle=False, num_workers=self.num_workers,
                        pin_memory=self.pin_memory)
                        #collate_fn=data.collate_batch, pin_memory=self.pin_memory)
        for batch in tqdm(dl, leave=False, ncols=100, mininterval=1, desc='Predict'):
            batch['batch_size'] = len(batch['user_id'])
            prediction = model(utils.batch_to_gpu(utils.batch_to_gpu(batch), model._device))
            predictions.extend(prediction.cpu().data.numpy())
        return np.array(predictions)


    ########################## methods for MeLON ###########################

    def print_res(self,
                  model: torch.nn.Module,
                  data_dict: Dict[str, MetaModel.Dataset],
                  args,
                  meta_model) -> str:
        """
        Construct the final result string before/after training
        :return: test result string
        """
        _, _, result_dict_list = self.fit_online(model, data_dict['test'], args, meta_model)
        res_str_first = '(' + utils.format_metric(result_dict_list[0]) + ')'
        res_str_last = '(' + utils.format_metric(result_dict_list[-1]) + ')'
        return ' '.join((res_str_first, res_str_last))
    
    def make_plot(self, args, data, name, snap_idx=0):
        y = data
        x = range(len(y))
        plt.plot(x, y)
        plt.xlabel('epoch')
        plt.ylabel('{}'.format(name))
        plt.title('{}_{}'.format(name, snap_idx))
        plt.savefig(args.test_result_file+'_{}_{}.png'.format(name, snap_idx))
        plt.close()

    def train(self,
              model: torch.nn.Module,
              data_dict: Dict[str, MetaModel.Dataset],
              args,
              snap_idx=0) -> NoReturn:

        if model.optimizer is None:
            model.optimizer = self._build_optimizer(model)

        self._check_time(start=True)

        loss_list, meta_loss_list, eval_loss_list = list(), list(), list()
        self.time_d = {}
        fair_loss_list, pd_list, lambda_list = list(), list(), list()

        try:
            for epoch in tqdm(range(self.epoch), ncols=100, mininterval=1):
            #for epoch in range(self.epoch):

                self._check_time()


                if 'finetune' in self.dyn_method and os.path.exists(model.model_path+'_snap{}'.format(0)):
                    print('Already trained: {}'.format(model.model_path+'_snap{}'.format(0)))
                    model.load_model(add_path='_snap{}'.format(0))
                    break

                loss, meta_loss, ori_loss, fair_loss, pd, lambda_, flag = self.fit_offline(model, data_dict['train'],
                                               args, epoch=epoch + 1)
                training_time = self._check_time()

                # Print first and last loss/test
                logging.info("Epoch {:<3} loss={:<.4f} ori_loss={:<.4f} fair_loss={:<.4f} [{:<.1f} s] ".format(
                             epoch + 1, loss, ori_loss, fair_loss, training_time))
                if flag:
                    logging.info('NaN loss, stop training')
                    break

                fair_loss_list.append(fair_loss)
                pd_list.append(pd)
                lambda_list.append(lambda_)


                # Observe selected tensors
                # if len(model.check_list) > 0 and self.check_epoch > 0 and epoch % self.check_epoch == 0:
                #     utils.check(model.check_list)




                # if flag:
                #     #logging.info('\n\n{}'.format(test_results))
                #     logging.info("@@@ Nonzero prediction continues, early stop training @@@")
                #     logging.info(os.linesep + '-' * 45 + ' END: ' + utils.get_time() + ' ' + '-' * 45)
                #     exit(1)

        except KeyboardInterrupt:
            logging.info("Early stop manually")
            exit_here = input("Exit completely without evaluation? (y/n) (default n):")
            if exit_here.lower().startswith('y'):
                logging.info(os.linesep + '-' * 45 + ' END: ' + utils.get_time() + ' ' + '-' * 45)
                exit(1)

        #with open(args.test_result_file+'_loss.txt', 'w+') as f:

        # y = fair_loss_list
        # x = range(len(y))
        # plt.plot(x, y)
        # plt.xlabel('epoch') 
        # plt.ylabel('fair_loss')
        # plt.title('fair_loss_0_pretrain')
        # plt.savefig(args.test_result_file+'_fair_loss_0.png')
        # plt.close()

        self.make_plot(args, pd_list, 'pd', snap_idx=0)
        # self.make_plot(args, lambda_list, 'lambda', snap_idx=0)
        # y = pd_list
        # x = range(len(y))
        # plt.plot(x, y)
        # plt.xlabel('epoch')
        # plt.ylabel('pd')
        # plt.title('pd_0_pretrain')
        # plt.savefig(args.test_result_file+'_pd_0.png')
        # plt.close()

        # self.global_pd_diff = []
        # self.global_pd = {}
        # self.global_pd_diff.append(pd_list[-1])
        # self.get_pd(self.global_pd, model, 0)


        logging.info('dyn_method: {}'.format(self.dyn_method))
        # Full re-training
        if 'fulltrain' in self.dyn_method:
            model.save_model(add_path='_snap{}'.format(snap_idx))
            return self.time[1] - self.time[0]
        # pre-training
        if 'pretrain' in self.dyn_method:
            for snap_idx in range(len(self.snap_boundaries)):
                model.save_model(add_path='_snap{}'.format(snap_idx))             
        # fine-tuning
        else:
            if 'modi-fine' in self.dyn_method:
                for idx in range(snap_idx+1):
                    model.save_model(add_path='_snap{}'.format(idx))

            model_ = copy.deepcopy(model) ###
            #model.save_model(add_path='_train') 

            self.time_d['pre-train'] = self.time[1] - self.time[0]
            model.save_model(add_path='_snap{}'.format(0))
            
            flag = self.dynamic_prediction(model_,
                                            data_dict['test'],
                                            args, epoch=epoch + 1)
            with open(args.test_result_file+'_time_test.txt', 'w+') as f:
                for k, v in self.time_d.items():
                    f.writelines('{}\t'.format(k))
                f.writelines('\n')
                for k, v in self.time_d.items():
                    f.writelines('{:.4f}\t'.format(v))
                f.writelines('\n')
                for k, v in self.time_d.items(): 
                    f.writelines('{:.4f}\t'.format(v/60))

        # final = {}
        # final['pd_diff'] = self.global_pd_diff
        # #self.global_pd_diff
        # #self.global_pd # per test setting per metric

        # for setting, metric in self.global_pd.items():
        #     for m, v in metric.items():
        #         final['test={}_metric={}'.format(setting, m)] = v


        # with open(args.test_result_file+'_a_final_pd', 'w+') as f:
        #     for k, v in final.items():
        #         f.writelines('{}\t'.format(k))
        #         for i in v:
        #             f.writelines('{:.4f}\t'.format(i))
        #         f.writelines('\n')


        # for k, v in final.items():
        #     plt.plot(range(len(v)), v, label=k)
        #     plt.xlabel('time period')
        #     plt.ylabel('pd')
        #     plt.title(k)
        #     plt.legend()
        #     plt.savefig(args.test_result_file+'_a_'+k+'.png')
        #     plt.close()

        # test_settings = ['current','remain','fixed','next']
        # for setting in test_settings:
        #     for k, v in final.items():
        #         if 'pd_diff' in k or setting in k:
        #             plt.plot(range(len(v)), v, label=k)
           
        #     plt.xlabel('time period')
        #     plt.ylabel('pd')
        #     plt.title('pd_{}'.format(setting))
        #     plt.legend()
        #     plt.savefig(args.test_result_file+'_a_{}.png'.format(setting))
        #     plt.close()


        logging.info(os.linesep + "[{:<.1f} m] ".format((self.time[1] - self.time[0]) / 60))

    def fit_offline(self,
                    model: torch.nn.Module,
                    data: MetaModel.Dataset,
                    args,
                    epoch=-1) -> float:

        gc.collect()
        torch.cuda.empty_cache()

        loss_lst, meta_loss_lst, ori_loss_lst, fair_loss_lst = list(), list(), list(), list()
        pd_list = list()
        lambda_list = list()
        dl = DataLoader(data, batch_size=1, shuffle=True, num_workers=self.num_workers, pin_memory=self.pin_memory)
        
        #for current in tqdm(dl, leave=True, desc='Epoch {:<3}'.format(epoch), ncols=100, mininterval=1):
        flag = 0
        for current in dl:
            current = utils.batch_to_gpu(utils.squeeze_dict(current), model._device)
            current['batch_size'] = len(current['user_id'])
            loss, prediction, ori_loss, fair_loss, pd, lambda_ = self.train_recommender_vanilla(model, current, data)

            loss_lst.append(loss)
            ori_loss_lst.append(ori_loss)
            if fair_loss is not None:
                fair_loss_lst.append(fair_loss)
            if pd is not None:
                pd_list.append(pd)
            if lambda_ is not None:
                lambda_list.append(lambda_)

            flag = np.isnan(prediction).any()
            if flag: 
                break

        return np.mean(loss_lst).item(), np.mean(meta_loss_lst).item(), np.mean(ori_loss_lst).item(), np.mean(fair_loss_lst).item(), np.mean(pd_list).item(), np.mean(lambda_list).item(), flag



    def dynamic_prediction(self,
                model: torch.nn.Module,
                #data_dict: Dict[str, MetaModel.Dataset],
                data: MetaModel.Dataset,
                args,
                epoch=-1) -> float:

        self._check_time()

        gc.collect()
        torch.cuda.empty_cache()

        starts = []
        ends = []
        for i in range(len(self.snap_boundaries)):
            if i == 0:
                starts.append(-100)
            else:
                starts.append(self.snap_boundaries[i-1])
            ends.append(self.snap_boundaries[i])

        snap_idx = 0
        data_custom = {}
        for start, end in zip(starts, ends):
            data_custom[snap_idx] = []
            data.fine_tune_snap_idx = snap_idx

            # if snap_idx == 0:
            #     snap_idx += 1
            #     continue

            dl = DataLoader(data, batch_size=1, shuffle=False, num_workers=0, pin_memory=self.pin_memory)
            for i, current in enumerate(dl):
                ### 230220
                if i < start:
                    continue

                if i >= end:
                    break

                #print(i)
                current = utils.batch_to_gpu(utils.squeeze_dict(current), model._device)
                current['batch_size'] = len(current['user_id'])
                data_custom[snap_idx].append(current)
            #print(data_custom[snap_idx])
            snap_idx += 1

        t = self._check_time()
        logging.info('test batch collecting: {} s'.format(t))
        self.time_d['test batch collecting'] = t

        snap_thres = re.sub('[^0-9]', '', args.dyn_method)
        if snap_thres == '':
            snap_thres = -1 # default
        else:
            snap_thres = int(snap_thres)

        if 'modi-fine' in args.dyn_method:
            snap_thres = snap_thres
            # fine-tuning starts from the next snapshot
        flag = 0
        for snap_idx, snapshot_data in data_custom.items():
            logging.info('snap_idx: {}'.format(snap_idx))
            #print(snapshot_data)
            
            # snap_idx == 0 -> pretrain data -> skip
            if snap_idx == 0:
                continue
            # ignore up to time period "snap_thres" 
            if snap_idx > snap_thres:
                over_fair_loss_lst = list()
                over_pd_list = list()
                over_lambda_list = list()
                for e in tqdm(range(self.tepoch), desc='Until {:<3}'.format(ends[snap_idx])):
                    gc.collect()
                    torch.cuda.empty_cache()

                    loss_lst = list()
                    ori_loss_lst = list()
                    fair_loss_lst = list()
                    pd_list = list()
                    lambda_list = list()
                    for i, current in enumerate(snapshot_data):
                        loss, prediction, ori_loss, fair_loss, pd, lambda_ = self.train_recommender_vanilla(model, current, data)       
                        loss_lst.append(loss)
                        ori_loss_lst.append(ori_loss)
                        if fair_loss is not None:
                            fair_loss_lst.append(fair_loss)
                        if pd is not None:
                            pd_list.append(pd)
                        if lambda_ is not None:
                            lambda_list.append(lambda_)

                        flag = np.isnan(prediction).any()
                        if flag: 
                            break

                        # if e == self.tepoch-1:
                        #     data.update_historical_degree(current['user_id'], current['item_id'][:,0])   

                    logging.info("Epoch {:<3} loss={:<.4f} ori_loss={:<.4f} fair_loss={:<.4f} ".format(
                             e + 1, np.mean(loss_lst).item(), np.mean(ori_loss_lst).item(), np.mean(fair_loss_lst).item()))
                    over_fair_loss_lst.append(np.mean(fair_loss_lst).item())
                    over_pd_list.append(np.mean(pd_list).item())
                    over_lambda_list.append(np.mean(lambda_list).item())
                    
                    if flag:
                        logging.info('@@@ prediction contains invalid values @@@')
                        flag = 0
                        break
                    

                self.make_plot(args, over_pd_list, 'pd', snap_idx)
                #self.make_plot(args, over_lambda_list, 'lambda', snap_idx)



            # Save the model snapshots
            if 'modi-fine' in args.dyn_method and snap_idx <= snap_thres:
                pass
            else:
                model.save_model(add_path='_snap{}'.format(snap_idx))

            # update user/item occurrence time information
            #data.update_occurrence_time()

            self.time_d['period_{}'.format(snap_idx)] = self._check_time()
       

        # return nan for the meta_loss_lst, if not dyn_update mode
        return flag
        #np.mean(loss_lst).item(), flag,  eval_result

    def train_recommender_vanilla(self, model, current, data):
        # Train recommender
        model.train()
        # Get recommender's prediction and loss from the ``current'' data at t
        prediction = model(current['user_id'], current['item_id'], self.DRM)
        
        loss, ori_loss, fair_loss, pd, lambda_ = model.loss(prediction, current, data, reduction='mean')


        # Update the recommender
        model.optimizer.zero_grad()
        loss.backward()
        model.optimizer.step()

        if fair_loss is not None:
            fair_loss = fair_loss.cpu().data.numpy()
        if pd is not None:
            pd = pd.cpu().data.numpy()
        if lambda_ is not None:
            lambda_ = lambda_.cpu().data.numpy()

        return loss.cpu().data.numpy(), prediction.cpu().data.numpy(), ori_loss.cpu().data.numpy(), fair_loss, pd, lambda_

    def get_pd(self, global_pd, model, snap_idx):
        #tester = Tester(args, corpus)

        # tester.dp(args, model)
        # dict of dict / pd_per_test_setting_per_metric
        pd_per_test_setting = self.tester.quick_get_pd(model, snap_idx)
        for test_setting, pd_per_metric in pd_per_test_setting.items():
            if test_setting not in global_pd:
                global_pd[test_setting] = dict()
            for metric, pd in pd_per_metric.items():
                if metric not in global_pd[test_setting]:
                    global_pd[test_setting][metric] = list()
                global_pd[test_setting][metric].append(pd)




# Tester implemented by HY
class Tester(object):
    @staticmethod
    def parse_tester_args(parser):
        parser.add_argument('--test_topk', type=str, default='[20]',
                            help='The number of items recommended to each user.')
        parser.add_argument('--test_metric', type=str, default='["recall","ndcg1","ap0","mrr1","f1","hit_ratio","hit","mrr0","precision"]',
                            help='metrics: NDCG, HR')
        parser.add_argument('--test_result_file', type=str, default='',
                            help='')

        return parser


    def quick_recommendation(self, model, train_file, test_file, snap_idx):
        topk = self.K
        num_neg_samples = self.num_neg_samples

        self.init_results()
        self.set_user_attr(train_file, self.user_attr_file)
        self.num_type_attr = len(self.user_attr[0])

        # For each user, there are personalized items in the recommendation list and test positive items
        # K = max(topk)
        train_edges = utils.read_data_from_file_int(train_file)
        test_edges = utils.read_data_from_file_int(test_file)
        train_pos = utils.get_user_dil_from_edgelist(train_edges)
        test_pos = utils.get_user_dil_from_edgelist(test_edges)
        train_user_set, train_item_set = utils.get_user_item_set(train_edges)
        test_user_set, test_item_set = utils.get_user_item_set(test_edges)

        # Do not test new users, which does not exist in the training set
        # Generate top-k recommendation list for each user
        random.seed(10)
        for user in train_user_set:
            # Skip if the user is not in the test set
            if user in test_pos.keys():
                recommendation_list, num_unseen_items = self.generate_recommendation_list_for_a_user(model, user, train_item_set, train_pos, test_pos, topk, num_neg_samples)
                self.measure_performance_for_a_user(user, recommendation_list, train_pos, test_pos, num_unseen_items)

        # print(self.num_unseen_items_per_group)

        self.average_user()
        self.average_user_attr()
        self.count_info_per_group(train_user_set, self.num_actual_users_per_group)
        self.average_info_per_group(self.num_unseen_items_per_group)
        self.average_info_per_group(self.num_test_pos_per_group)
        self.average_info_per_group(self.num_train_pos_per_group)
        num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.num_actual_train_pos_per_group, self.num_actual_users_per_group, train_pos)

        #result_str = self.get_results_str_()
        self.measure_unfairness()
        result_str = ''

        pd_dict = {}
        for k in range(self.num_type_attr):
            if self.attr_type[k] == 'ages' or self.attr_type[k] == 'occupations':
                continue
            for metric in self.metrics:
                if metric in ['recall','f1','ndcg1','precision']:
                    pd_dict[metric] = self.binary_unfairness[metric][k]
                #result_str += '\n\n@@@ Unfairness values @@@'
                # result_str += '\n{}\t{:.4f}'.format(metric+'__overall', self.results[metric])

                # if self.binary_unfairness[metric].get(k) is not None:
                #     result_str += '\n{}\t{:.4f}'.format(metric+'__'+self.attr_type[k], self.binary_unfairness[metric][k])
                #     for attr in self.user_groups[k]:
                #         result_str += '\n{}\t{:.4f}'.format(metric+'__'+str(attr), self.results_user_attr[k][attr][metric])


                # else:
                #     for attr in self.user_groups[k]:
                #         result_str += '\n{}\t{:.4f}'.format(metric+'__'+str(attr), self.results_user_attr[k][attr][metric])


        return pd_dict
    
    def quick_get_pd(self, model, snap_idx):
        # Test settings: 1. remaining, 2. fixed, 3. live-stream (predict right next interactions)
        # if torch.cuda.is_available():
        #     model.to(model._device)

        test_settings = ['current', 'remain', 'fixed', 'next']
        pd_per_test_setting = {}

        for setting in test_settings:
            #for snap_idx in range(len(self.snap_boundaries)):

            #model.load_model(add_path='_snap{}'.format(snap_idx), flag=1)
            model.eval()

            train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format(setting, snap_idx))
            test_file = os.path.join(self.snapshots_path, '{}_test_snap{}'.format(setting, snap_idx))

            if setting == 'current':
                train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format('remain', snap_idx))
                if snap_idx == 0:
                    test_file = train_file
                else:
                    test_file = os.path.join(self.snapshots_path, '{}_test_snap{}'.format('next', snap_idx-1))


            pd_per_metric = self.quick_recommendation(model, train_file, test_file, snap_idx)
            pd_per_test_setting[setting] = pd_per_metric

            #result_filename_ = os.path.join(self.test_result_file, '{}_snap{}.txt'.format(setting, snap_idx))

        return pd_per_test_setting
    

    def dp(self, args, model):

        # Test settings: 1. remaining, 2. fixed, 3. live-stream (predict right next interactions)
        if torch.cuda.is_available():
            model.to(model._device)

        test_settings = ['remain', 'fixed', 'next']

        for topk in self.topk:

            for setting in test_settings:
                for snap_idx in range(len(self.snap_boundaries)):
                    # train_data = torch.load(os.path.join(self.snapshots_path, 'remain_train_snap'+str(idx)))
                    # test_data = torch.load(os.path.join(self.snapshots_path, 'remain_test_snap'+str(idx)))

                    # if os.path.exists(os.path.join(self.test_result_file, '{}_snap{}.txt'.format(setting, len(self.snap_boundaries)-1))):
                    #     print('Already existing test files: {}'.format(os.path.join(self.test_result_file, '{}_snap{}.txt'.format(setting, len(self.snap_boundaries)-1))))
                    #     break



                    model.load_model(add_path='_snap{}'.format(snap_idx), flag=1)
                    model.eval()

                    train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format(setting, snap_idx))
                    test_file = os.path.join(self.snapshots_path, '{}_test_snap{}'.format(setting, snap_idx))
                    if snap_idx == 0:
                        hist_train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format(setting, snap_idx))
                        current_train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format(setting, snap_idx))
                    else:
                        hist_train_file = os.path.join(self.snapshots_path, '{}_train_snap{}'.format(setting, snap_idx-1))
                        current_train_file = os.path.join(self.snapshots_path, '{}_test_snap{}'.format('next', snap_idx-1))

                    # if args.dyn_update == -2:
                    #     train_file = hist_train_file

                    result_str, info_str = self.recommendation(model, train_file, test_file, hist_train_file, current_train_file, snap_idx, topk)

                    result_filename_ = os.path.join(self.test_result_file, '{}_{}_snap{}.txt'.format(topk, setting, snap_idx))
                    r_string = 'Top {} Results'.format(topk) + result_str #'\n\n\n\n' + info_str 
                    with open(result_filename_, 'w+') as f:
                        f.writelines(r_string)



                # mean values over snapshots
                d = {}
                for snap_idx in range(len(self.snap_boundaries)):
                    with open(os.path.join(self.test_result_file, '{}_{}_snap{}.txt'.format(topk, setting, snap_idx)), 'r') as f:
                        #lines = f.readlines()[1:4*len(self.metrics)+1+24] #len(self.metrics)
                        lines = f.readlines()[1:] #len(self.metrics)
                        data = [line.replace('\n','').split() for line in lines]

                        cnt = 0
                        for value in data:
                            if d.get(value[0]) is None:
                                d[value[0]] = []
                            if cnt >= 4*len(self.metrics):
                                d[value[0]].append(int(value[1]))
                            else:
                                d[value[0]].append(float(value[1]))
                            cnt += 1

                # Write mean values
                with open(os.path.join(self.test_result_file, '0_{}_mean_{}.txt'.format(topk, setting)), 'w+') as f:
                    cnt = 0
                    for k, v in d.items():
                        cnt += 1
                        # if cnt == 4*3+1:
                        #     f.writelines('\n\n')
                            
                        f.writelines('{}\t{}\n'.format(k, sum(v)/len(v)))

                # Write list of values (trend over time)
                with open(os.path.join(self.test_result_file, '0_{}_trend_{}.txt'.format(topk, setting)), 'w+') as f:
                    #cnt = 0
                    for k, v in d.items():
                        # cnt += 1
                        # if cnt == 4*3+1:
                        #     f.writelines('\n\n')
                        f.writelines('{}'.format(k))
                        for v_ in v:
                            f.writelines('\t{}'.format(v_))
                        f.writelines('\n')


                # # Write mean values
                # with open(os.path.join(self.test_result_file, '0_{}_abs_mean_{}.txt'.format(topk, setting)), 'w+') as f:
                #     cnt = 0
                #     for k, v in d.items():
                #         cnt += 1
                #         if cnt == 4*4+1:
                #             f.writelines('\n\n')
                        
                #         v = [abs(v_) for v_ in v]
                #         f.writelines('{}\t{:.4f}\n'.format(k, sum(v)/len(v)))

                # # Write list of values (trend over time)
                # with open(os.path.join(self.test_result_file, '0_{}_abs_trend_{}.txt'.format(topk, setting)), 'w+') as f:
                #     cnt = 0
                #     for k, v in d.items():
                #         cnt += 1
                #         if cnt == 4*4+1:
                #             f.writelines('\n\n')
                #         f.writelines('{}'.format(k))
                #         for v_ in v:
                #             f.writelines('\t{}'.format(abs(v_)))
                #         f.writelines('\n')



                #self.make_plot(d, self.K, os.path.join(self.test_result_file, '{}_snap'.format(setting)), setting)




    def make_plot(self, d, topk, result_file, setting):

        for k, v in d.items():
            x = range(len(self.snap_boundaries))
            y = v
            # plt.figure(figsize=(10,10))
            plt.plot(x, y)
            plt.xlabel('time')
            plt.ylabel(k)
            plt.title('top{}_{}_{}'.format(topk, setting, k))
            #plt.show()

            # filename = './plots'
            # if not os.path.exists(filename):
            #   os.mkdir(filename)
            filename = result_file + '_{}'.format(k)
            plt.savefig(filename)
            plt.close()       




    def __init__(self, args, corpus):
        self.user_attr_file = corpus.user_attr_path
        self.snap_boundaries = corpus.snap_boundaries
        self.snapshots_path = corpus.snapshots_path
        self.num_neg_samples = 100
        if args.dataset == 'Modcloth':
            self.num_neg_samples = 100

        self.test_result_file = args.test_result_file
        # self.dyn_update = args.dyn_update


        self.topk = eval(args.test_topk)
        self.K = self.topk[0]
        self.metrics = [m.strip() for m in eval(args.test_metric)]
        #self.main_metric = '{}@{}'.format(self.metrics[0], self.topk[0])  # early stop based on main_metric

        print('Test start: topk: {}, metric: {}'.format(self.topk, self.metrics))
        # print(self.topk)
        # print(self.metrics)

        # self.data_info = data_info
        # self.metrics = ['recall','ndcg','hit_ratio','precision','f1']
        # self.metrics = ['recall','ndcg','precision','f1']

        # MovieLens-1M: i=0: gender, i=1: age, i=2: occupation
        binary = list(range(2)) # ['M'=0,'F'=1]
        # ages = [18,25,35,45,50,56]
        # occupations = list(range(21))
        # outdegree = ['H','L']
        # self.attr_type = ['genders', 'ages', 'occupations', 'out-degree-50%', 'out-degree-5%']
        # self.user_groups = [genders, ages, occupations, outdegree, outdegree]

        self.num_groups_degree = 10
        fine_grained_outdegree = list(range(self.num_groups_degree))

        #self.attr_type = ['genders', 'out-degree_{}'.format(self.num_groups_degree)]
        if args.dataset == 'Kuairec':
            self.attr_type = ['user_active_degree']
        else:
            self.attr_type = ['genders']
        #self.user_groups = [genders, fine_grained_outdegree]

        self.user_groups = [binary]

        #self.set_user_attr(train_file, user_attr_file, self.num_groups_degree)
        self.num_type_attr = 1
        

    def set_user_attr(self, train_file, user_attr_file):
        train_edges = utils.read_data_from_file_int(train_file)
        train_user_set, _ = utils.get_user_item_set(train_edges)
        # self.outdegree = self.get_user_degree(train_edges)
        # degree_thres = self.get_degree_threshold(self.outdegree, 0.5)
        # degree_thres2 = self.get_degree_threshold(self.outdegree, 0.05)

        #degree_info = self.divide_users_by_degree(self.outdegree, num=num_groups_degree)

        # MovieLenz
        user_attr = utils.read_data_from_file_int(user_attr_file)
        user_attr_dict = {}
        for user in user_attr:
            #user_attr_dict[user[0]] = [user[1], user[2], user[3]]
            user_attr_dict[user[0]] = [user[1]]

        self.user_attr = {}
        #print('length: {}'.format(len(train_user_set)))
    

        for u_idx in train_user_set:
            #self.user_attr[u_idx] = [user_attr_dict[u_idx][0],user_attr_dict[u_idx][1],user_attr_dict[u_idx][2]]
            #self.user_attr[u_idx] = [user_attr_dict[u_idx][0], degree_info[self.outdegree[u_idx]]]
            self.user_attr[u_idx] = [user_attr_dict[u_idx][0]]
            # if self.outdegree[u_idx] >= degree_thres: 
            #     self.user_attr[u_idx].append('H')
            #     #print('H: {} / thres: {}'.format(self.outdegree[u_idx], degree_thres))
            # else:
            #     self.user_attr[u_idx].append('L')
            #     #print('L: {} / thres: {}'.format(self.outdegree[u_idx], degree_thres))

            # if self.outdegree[u_idx] >= degree_thres2:
            #     self.user_attr[u_idx].append('H')
            # else:
            #     self.user_attr[u_idx].append('L')


    def get_user_degree(self, edges):
        d = {}
        for edge in edges:
            if d.get(edge[0]) is None:
                d[edge[0]] = 0
            d[edge[0]] += 1

        return d

    def get_degree_threshold(self, degrees, threshold):
        d_list = []
        for i, d in degrees.items():
            d_list.append(d)
        d_list.sort(reverse = True)
        threshold = int(len(d_list)*threshold) - 1
        return d_list[threshold]

    def divide_users_by_degree(self, degrees, num=10):
        degree_list = list(degrees.values())
        degree_list.sort()

        std = round(len(degrees) / num)
        d_idx = std
        degree_pivots = []
        for cnt, degree in enumerate(degree_list):
            if cnt == d_idx:
                degree_pivots.append(degree)
                # group_class += 1
                d_idx += std
            if len(degree_pivots) == num-1:
                break

        degree_pivots.append(max(degree_list))
        # print(degree_pivots)
        # print('@@@@@@@@@@@@@')

        d = {}
        idx = 0
        for degree in range(1, max(degree_list)+1):

            if degree <= degree_pivots[idx]:
                d[degree] = idx
            else:
                idx += 1
                d[degree] = idx

        self.fine_grained_outdegree = degree_pivots

        return d

    def init_results(self):

        self.results = {k: 0.0 for k in self.metrics}
        self.num_test_users = 0

        self.results_user_attr = []
        self.num_users_per_group = []
        self.fairness_results = []
        for k in range(self.num_type_attr):
            self.results_user_attr.append({})
            self.num_users_per_group.append({})
            for attr in self.user_groups[k]:
                self.results_user_attr[k][attr] = {}
                for metric in self.metrics:
                    self.results_user_attr[k][attr][metric] = 0
                self.num_users_per_group[k][attr] = 0

        self.num_actual_users_per_group = copy.deepcopy(self.num_users_per_group)
        self.num_unseen_items_per_group = copy.deepcopy(self.num_users_per_group)
        self.num_test_pos_per_group = copy.deepcopy(self.num_users_per_group)
        self.num_train_pos_per_group = copy.deepcopy(self.num_users_per_group)
        self.num_actual_train_pos_per_group = copy.deepcopy(self.num_users_per_group)

        self.hist_num_actual_users_per_group = copy.deepcopy(self.num_users_per_group)
        self.hist_num_actual_train_pos_per_group = copy.deepcopy(self.num_users_per_group)
        self.cur_num_actual_users_per_group = copy.deepcopy(self.num_users_per_group)
        self.cur_num_actual_train_pos_per_group = copy.deepcopy(self.num_users_per_group)
        self.cold_start_num_actual_users_per_group = copy.deepcopy(self.num_users_per_group)
        self.cold_start_num_actual_train_pos_per_group = copy.deepcopy(self.num_users_per_group)
        self.not_cold_start_num_actual_users_per_group = copy.deepcopy(self.num_users_per_group)
        self.not_cold_start_num_actual_train_pos_per_group = copy.deepcopy(self.num_users_per_group)
        

    def generate_recommendation_list_for_a_user(self, model, user, train_item_set, train_pos, test_pos, K, num_neg_samples=-1):

        #num_neg_samples = 100
        verbose = False
        # if user == 0:
        #   verbose = True

        new_item_set = list(set(train_item_set) - set(train_pos[user]) - set(test_pos[user]))

        if num_neg_samples != -1:
            if num_neg_samples > len(new_item_set):
                print(len(new_item_set))
                print('numer of train_item_set {}'.format(len(train_item_set)))
                print('number of items of the user {}'.format(len(train_pos[user])))
                print('number of test pos items of the user {}'.format(len(test_pos[user])))
            neg_samples = random.sample(new_item_set, num_neg_samples)
        else:
            neg_samples = new_item_set

        #print('check@@@ {}'.format(train_item_set+neg_samples))
        relevances = {}
        cnt = 0

        # In case of unseen test items, just use random embeddings of the model.
        # (n_items)
        candidate_items = test_pos[user] + neg_samples

        user_ = torch.from_numpy(np.array(user))
        candidate_items_ = torch.from_numpy(np.array(candidate_items))
        if torch.cuda.is_available():
            user_ = user_.to(model._device)
            candidate_items_ = candidate_items_.to(model._device)

        item_relevances = model.get_relevances(model, user_, candidate_items_)

        for item, relevance in zip(candidate_items, item_relevances):
            if item in train_item_set:
                relevances[item] = relevance
                #relevances[item] = train_embeddings[0][user] @ train_embeddings[1][item]
            else:
                relevances[item] = relevance
                cnt += 1

        sorted_relevances = sorted(relevances.items(), key=lambda x: x[1], reverse=True)

        if K > 0:
            recommendation_list = [rel[0] for rel in sorted_relevances][:K]
        else:
            recommendation_list = [rel[0] for rel in sorted_relevances]
        
        if verbose:
            print(recommendation_list)
            print('@@@CHECK length of rec list: {} = {}, no test item{}'.format(len(recommendation_list), K, cnt))

        return recommendation_list, cnt

    # @staticmethod
    # def evaluate_method(predictions: np.ndarray, topk: list, metrics: list) -> Dict[str, float]:
    #     """
    #     :param predictions: (-1, n_candidates) shape, the first column is the score for ground-truth item
    #     :param topk: top-K values list
    #     :param metrics: metrics string list
    #     :return: a result dict, the keys are metrics@topk
    #     """
    #     evaluations = dict()
    #     sort_idx = (-predictions).argsort(axis=1)
    #     gt_rank = np.argwhere(sort_idx == 0)[:, 1] + 1
    #     for k in topk:
    #         hit = (gt_rank <= k)
    #         for metric in metrics:
    #             key = '{}@{}'.format(metric, k)
    #             if metric == 'HR':
    #                 evaluations[key] = hit.mean(dtype=np.float16)
    #             elif metric == 'NDCG':
    #                 evaluations[key] = (hit / np.log2(gt_rank + 1)).mean(dtype=np.float16)
    #             else:
    #                 raise ValueError('Undefined evaluation metric: {}.'.format(metric))
    #     return evaluations

    def measure_performance_for_a_user(self, user, recommendation_list, train_pos, test_pos, num_unseen_items):
        flag = 0

        for metric in self.metrics:

            if test_pos.get(user) is not None:

                if metric == 'recall':
                    value = self.measure_recall(recommendation_list, test_pos[user])

                elif metric == 'ndcg1':
                    value = self.measure_ndcg(recommendation_list, test_pos[user], method=1)
                elif metric == 'ndcg0':
                    value = self.measure_ndcg_deprecated(recommendation_list, test_pos[user])
                elif metric == 'mrr0':
                    value = self.measure_mrr(recommendation_list, test_pos[user], method=0)
                elif metric == 'mrr1':
                    value = self.measure_mrr(recommendation_list, test_pos[user], method=1)
                elif metric == 'ap0':
                    value = self.measure_average_precision(recommendation_list, test_pos[user], method=0)
                elif metric == 'ap1':
                    value = self.measure_average_precision(recommendation_list, test_pos[user], method=1)
                elif metric == 'ap2':
                    value = self.measure_average_precision(recommendation_list, test_pos[user], method=2)

                elif metric == 'hit_ratio':
                    value = self.measure_hit_ratio(recommendation_list, test_pos[user])
                elif metric == 'precision':
                    value = self.measure_precision(recommendation_list, test_pos[user])
                elif metric == 'f1':
                    value = self.measure_f1(recommendation_list, test_pos[user])

                elif metric == 'ndcg2':
                    value = self.measure_ndcg(recommendation_list, test_pos[user], method=2)
                elif metric == 'hit':
                    value = self.measure_num_hit(recommendation_list, test_pos[user])

                else:
                    raise ValueError('Undefined evaluation metric: {}.'.format(metric))

                self.results[metric] += value
                if flag == 0:
                    self.num_test_users += 1

                # For each of user groups that are divided by sensitive attributes
                for k in range(self.num_type_attr):
                    for attr in self.user_groups[k]:
                        if self.user_attr[user][k] == attr:
                            self.results_user_attr[k][attr][metric] += value
                            # Is the user in the test set or not

                            if flag == 0:
                                self.num_users_per_group[k][attr] += 1
                                self.num_unseen_items_per_group[k][attr] += num_unseen_items
                                self.num_test_pos_per_group[k][attr] += len(test_pos[user])
                                self.num_train_pos_per_group[k][attr] += len(train_pos[user])
                
                flag += 1
            else:
                print('@@@@@@@@@error@@@@@@@@@@')


    def recommendation(self, model, train_file, test_file, hist_train_file, cur_train_file, snap_idx, topk=20, num_neg_samples=-1, method=0):
        topk = topk
        num_neg_samples = self.num_neg_samples

        self.init_results()
        self.set_user_attr(train_file, self.user_attr_file)
        #self.num_type_attr = len(self.user_attr[0])

        # For each user, there are personalized items in the recommendation list and test positive items
        # K = max(topk)
        train_edges = utils.read_data_from_file_int(train_file)
        test_edges = utils.read_data_from_file_int(test_file)
        train_pos = utils.get_user_dil_from_edgelist(train_edges)
        test_pos = utils.get_user_dil_from_edgelist(test_edges)
        train_user_set, train_item_set = utils.get_user_item_set(train_edges)
        test_user_set, test_item_set = utils.get_user_item_set(test_edges)


        # Do not test new users, which does not exist in the training set
        # Generate top-k recommendation list for each user
        # num_neg_samples = -1
        random.seed(10)
        for user in train_user_set:
            # Skip if the user is not in the test set
            if user in test_pos.keys():
                recommendation_list, num_unseen_items = self.generate_recommendation_list_for_a_user(model, user, train_item_set, train_pos, test_pos, topk, num_neg_samples)
                self.measure_performance_for_a_user(user, recommendation_list, train_pos, test_pos, num_unseen_items)

        # print(self.num_unseen_items_per_group)

        self.average_user()
        self.average_user_attr()
        self.count_info_per_group(train_user_set, self.num_actual_users_per_group)
        self.average_info_per_group(self.num_unseen_items_per_group)
        self.average_info_per_group(self.num_test_pos_per_group)
        self.average_info_per_group(self.num_train_pos_per_group)
        num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.num_actual_train_pos_per_group, self.num_actual_users_per_group, train_pos)

        info_str = ''
        # info_str = '@@@ User Groups @@@'
        # info_str += '\noverall_num_test_users: {}, overall_real_num_test_users: {}'.format(len(train_user_set), self.num_test_users)
        # # info_str += '\nThe number of unseen test items per group: {}'.format(self.num_unseen_items_per_group)
        # info_str += '\nThe number of test positive items per group: {}'.format(self.num_test_pos_per_group)
        # info_str += '\nThe number of (valid) users per group: {}'.format(self.num_users_per_group)
        # info_str += '\nThe number of (valid) train positive items per group: {}'.format(self.num_train_pos_per_group)
        # info_str += '\nThe number of actual users per group: {}'.format(self.num_actual_users_per_group)
        # info_str += '\nThe number of actual train positive items per group: {}'.format(self.num_actual_train_pos_per_group)


        result_str = self.get_results_str_()
        # data analysis: # users (accumulated, new), # training data (mean, total) / (accumluated, new)
        result_str += '\n#_users_{}\t{:d}'.format(self.user_groups[0][0], self.num_actual_users_per_group[0][0])
        result_str += '\n#_users_{}\t{:d}'.format(self.user_groups[0][1], self.num_actual_users_per_group[0][1])
        result_str += '\n#_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][0], self.num_actual_train_pos_per_group[0][0])
        result_str += '\n#_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][1], self.num_actual_train_pos_per_group[0][1])
        result_str += '\n#_train_pos_total_{}\t{:d}'.format(self.user_groups[0][0], num_actual_train_pos_per_group_total[0][0])
        result_str += '\n#_train_pos_total_{}\t{:d}'.format(self.user_groups[0][1], num_actual_train_pos_per_group_total[0][1])


        result_str += '\n#_(valid)_test_users_{}\t{:d}'.format(self.user_groups[0][0], self.num_users_per_group[0][0])
        result_str += '\n#_(valid)_test_users_{}\t{:d}'.format(self.user_groups[0][1], self.num_users_per_group[0][1])
        result_str += '\n#_(valid)_test_pos_mean_{}\t{:d}'.format(self.user_groups[0][0], self.num_test_pos_per_group[0][0])
        result_str += '\n#_(valid)_test_pos_mean_{}\t{:d}'.format(self.user_groups[0][1], self.num_test_pos_per_group[0][1])
        result_str += '\n#_(valid)_test_pos_total_{}\t{:d}'.format(self.user_groups[0][0], self.num_test_pos_per_group[0][0]*self.num_users_per_group[0][0])
        result_str += '\n#_(valid)_test_pos_total_{}\t{:d}'.format(self.user_groups[0][1], self.num_test_pos_per_group[0][1]*self.num_users_per_group[0][1])
        result_str += '\n#_(valid)_train_pos_{}\t{:d}'.format(self.user_groups[0][0], self.num_train_pos_per_group[0][0])
        result_str += '\n#_(valid)_train_pos_{}\t{:d}'.format(self.user_groups[0][1], self.num_train_pos_per_group[0][1])
    

        hist_train_edges = utils.read_data_from_file_int(hist_train_file)
        hist_train_pos = utils.get_user_dil_from_edgelist(hist_train_edges)
        hist_train_user_set, _ = utils.get_user_item_set(hist_train_edges)
        self.count_info_per_group(hist_train_user_set, self.hist_num_actual_users_per_group)
        #hist_num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.hist_num_actual_train_pos_per_group, self.hist_num_actual_users_per_group, hist_train_pos)
        result_str += '\n#_coldstart_users_{}\t{:d}'.format(self.user_groups[0][0], self.num_actual_users_per_group[0][0]-self.hist_num_actual_users_per_group[0][0])
        result_str += '\n#_coldstart_users_{}\t{:d}'.format(self.user_groups[0][1], self.num_actual_users_per_group[0][1]-self.hist_num_actual_users_per_group[0][1])

        

        cur_train_edges = utils.read_data_from_file_int(cur_train_file)
        cur_train_pos = utils.get_user_dil_from_edgelist(cur_train_edges)
        cur_train_user_set, _ = utils.get_user_item_set(cur_train_edges)
        self.count_info_per_group(cur_train_user_set, self.cur_num_actual_users_per_group)
        cur_num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.cur_num_actual_train_pos_per_group, self.cur_num_actual_users_per_group, cur_train_pos)

        cold_start_train_pos = {}
        not_cold_start_train_pos = {}

        if snap_idx == 0:
            cold_start_user_set = not_cold_start_user_set = train_user_set
        else:
            cold_start_user_set = list(set(train_user_set) - set(hist_train_user_set))
            not_cold_start_user_set = list(set(cur_train_user_set) - set(cold_start_user_set))

        for i, v in cur_train_pos.items():
            if i in cold_start_user_set:
                cold_start_train_pos[i] = v
            if i in not_cold_start_user_set:
                not_cold_start_train_pos[i] = v
        self.count_info_per_group(cold_start_user_set, self.cold_start_num_actual_users_per_group)
        self.count_info_per_group(not_cold_start_user_set, self.not_cold_start_num_actual_users_per_group)
        cold_start_num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.cold_start_num_actual_train_pos_per_group, self.cold_start_num_actual_users_per_group, cold_start_train_pos)
        not_cold_start_num_actual_train_pos_per_group_total = self.get_average_out_degree_per_groups(self.not_cold_start_num_actual_train_pos_per_group, self.not_cold_start_num_actual_users_per_group, not_cold_start_train_pos)
        result_str += '\n#_cold_start_users_{}\t{:d}'.format(self.user_groups[0][0], self.cold_start_num_actual_users_per_group[0][0])
        result_str += '\n#_cold_start_users_{}\t{:d}'.format(self.user_groups[0][1], self.cold_start_num_actual_users_per_group[0][1])
        result_str += '\n#_not_cold_start_users_{}\t{:d}'.format(self.user_groups[0][0], self.not_cold_start_num_actual_users_per_group[0][0])
        result_str += '\n#_not_cold_start_users_{}\t{:d}'.format(self.user_groups[0][1], self.not_cold_start_num_actual_users_per_group[0][1])
        result_str += '\n#_cold_start_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][0], self.cold_start_num_actual_train_pos_per_group[0][0])
        result_str += '\n#_cold_start_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][1], self.cold_start_num_actual_train_pos_per_group[0][1])
        result_str += '\n#_not_cold_start_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][0], self.not_cold_start_num_actual_train_pos_per_group[0][0])
        result_str += '\n#_not_cold_start_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][1], self.not_cold_start_num_actual_train_pos_per_group[0][1])
        result_str += '\n#_cold_start_train_pos_total_{}\t{:d}'.format(self.user_groups[0][0], cold_start_num_actual_train_pos_per_group_total[0][0])
        result_str += '\n#_cold_start_train_pos_total_{}\t{:d}'.format(self.user_groups[0][1], cold_start_num_actual_train_pos_per_group_total[0][1])
        result_str += '\n#_not_cold_start_train_pos_total_{}\t{:d}'.format(self.user_groups[0][0], not_cold_start_num_actual_train_pos_per_group_total[0][0])
        result_str += '\n#_not_cold_start_train_pos_total_{}\t{:d}'.format(self.user_groups[0][1], not_cold_start_num_actual_train_pos_per_group_total[0][1])


        result_str += '\n#_new_users_{}\t{:d}'.format(self.user_groups[0][0], self.cur_num_actual_users_per_group[0][0])
        result_str += '\n#_new_users_{}\t{:d}'.format(self.user_groups[0][1], self.cur_num_actual_users_per_group[0][1])
        result_str += '\n#_new_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][0], self.cur_num_actual_train_pos_per_group[0][0])
        result_str += '\n#_new_train_pos_mean_{}\t{:d}'.format(self.user_groups[0][1], self.cur_num_actual_train_pos_per_group[0][1])
        result_str += '\n#_new_train_pos_total_{}\t{:d}'.format(self.user_groups[0][0], cur_num_actual_train_pos_per_group_total[0][0])
        result_str += '\n#_new_train_pos_total_{}\t{:d}'.format(self.user_groups[0][1], cur_num_actual_train_pos_per_group_total[0][1])
        
        result_str += '\n#_new_train_pos_mean_over_all_users_{}\t{:d}'.format(self.user_groups[0][0], int(cur_num_actual_train_pos_per_group_total[0][0]/self.num_actual_users_per_group[0][0]))
        result_str += '\n#_new_train_pos_mean_over_all_users_{}\t{:d}'.format(self.user_groups[0][1], int(cur_num_actual_train_pos_per_group_total[0][1]/self.num_actual_users_per_group[0][1]))

        result_str += '\n#_overall_num_test_users\t{:d}'.format(len(train_user_set))
        result_str += '\n#_overall_(valid)_test_users\t{:d}'.format(self.num_test_users)

        # if topk == 20:
        #     self.make_plot(topk, result_file)

        return result_str, info_str

    # def make_plot(self, topk, result_file):
    #     for k in range(self.num_type_attr):
    #         if 'out-degree' in self.attr_type[k]:
    #             for metric in self.metrics:
    #                 if metric == 'recall':
    #                     x = self.user_groups[k]
    #                     y = []
    #                     for attr in self.user_groups[k]:
    #                         y.append(self.results_user_attr[k][attr][metric])

    #                     plt.figure(figsize=(10,10))
    #                     plt.plot(x, y)
    #                     plt.xlabel('user groups divided by degree')
    #                     plt.ylabel('accuracy')
    #                     plt.title('{}_{}@{} => {}'.format(self.attr_type[k], metric, topk, self.fine_grained_outdegree))
    #                     #plt.show()


    #                     # filename = './plots'
    #                     # if not os.path.exists(filename):
    #                     #   os.mkdir(filename)
    #                     filename = result_file + '_{}_{}@{}.png'.format(self.attr_type[k], metric, topk)
    #                     plt.savefig(filename)
    #                     plt.close()


    def get_results_str_(self):
        self.measure_unfairness()

        # result_str = '\n@@@ Overall results @@@'
        # for metric, value in self.results.items():
        #     result_str += '\n{}\t{}'.format(metric, value)

        result_str = ''

        for k in range(self.num_type_attr):
            if self.attr_type[k] == 'ages' or self.attr_type[k] == 'occupations':
                continue
            for metric in self.metrics:
                #result_str += '\n\n@@@ Unfairness values @@@'
                result_str += '\n{}\t{:.4f}'.format(metric+'__overall', self.results[metric])

                if self.binary_unfairness[metric].get(k) is not None:
                    result_str += '\n{}\t{:.4f}'.format(metric+'__'+self.attr_type[k], self.binary_unfairness[metric][k])
                    for attr in self.user_groups[k]:
                        result_str += '\n{}\t{:.4f}'.format(metric+'__'+str(attr), self.results_user_attr[k][attr][metric])


                else:
                    for attr in self.user_groups[k]:
                        result_str += '\n{}\t{:.4f}'.format(metric+'__'+str(attr), self.results_user_attr[k][attr][metric])

            # result_str += '\n\n'
            # result_str += '\n\n@@@ Accuracy per user group @@@'
            # for k in range(self.num_type_attr):
            #     result_str += '\n\nAttribute type: {}'.format(self.attr_type[k])
            #     result_str += '\nmetric\tgroup\tvalue'
            #     for attr in self.user_groups[k]:
            #         result_str += '\n{}\t{}\t{}'.format(metric, attr, self.results_user_attr[k][attr][metric])

        return result_str

    def measure_unfairness(self):
        self.variance = {}
        self.binary_unfairness = {}
        for metric in self.metrics:
            self.variance[metric] = []
            self.binary_unfairness[metric] = {}
            for k in range(self.num_type_attr):
                value_list = []
                for attr in self.user_groups[k]:
                    #if self.results_user_attr[k][attr][metric] != 0:
                    value_list.append(self.results_user_attr[k][attr][metric])
                self.variance[metric].append(np.var(value_list))

                if len(value_list) == 2:
                    self.binary_unfairness[metric][k] = value_list[0] - value_list[1]
        #print(self.results_user_attr)

    def count_info_per_group(self, train_user_set, info):
        for i in train_user_set:
            for k in range(self.num_type_attr):
                for attr in self.user_groups[k]:
                    if self.user_attr[i][k] == attr:
                        #self.num_actual_users_per_group[k][attr] += 1
                        info[k][attr] += 1

    def average_info_per_group(self, info):
        for k in range(self.num_type_attr):
            for attr in self.user_groups[k]:
                if self.num_users_per_group[k][attr] == 0:
                    info[k][attr] = 0
                else:
                    info[k][attr] = round(info[k][attr] / self.num_users_per_group[k][attr])

    def get_average_out_degree_per_groups(self, info, info_, outdegrees):
        
        for user, items in outdegrees.items():
            for k in range(self.num_type_attr):
                for attr in self.user_groups[k]:
                    if self.user_attr[user][k] == attr:
                        info[k][attr] += len(items)

        num_actual_train_pos_per_group_total = copy.deepcopy(info)

        for k in range(self.num_type_attr):
            for attr in self.user_groups[k]:
                if info_[k][attr] == 0:
                    info[k][attr] = 0
                else:
                    info[k][attr] = round(info[k][attr]/info_[k][attr])

        return num_actual_train_pos_per_group_total

    def average_user_attr(self):
        for k in range(self.num_type_attr):
            for attr in self.user_groups[k]:
                for metric in self.metrics:
                    if self.num_users_per_group[k][attr] == 0:
                        self.results_user_attr[k][attr][metric] = 0
                    else:
                        a = self.results_user_attr[k][attr][metric]
                        self.results_user_attr[k][attr][metric] /= self.num_users_per_group[k][attr]
                        # if self.results_user_attr[k][attr][metric] == 1:
                        #     print('dfadsfasdfdsfasdf')
                        #     print('self.num_users_per_group[k][attr]: ', self.num_users_per_group[k][attr])
                        #     print('self.results_user_attr[k][attr][metric]: ', a)

    def average_user(self):
        for metric, value in self.results.items():
            self.results[metric] = value/self.num_test_users

    def measure_recall(self, rec_list, test_pos):
        hit_count = np.isin(rec_list, test_pos).sum()

        return hit_count / len(test_pos)
     
    def measure_num_hit(self, rec_list, test_pos):
        return np.isin(rec_list, test_pos).sum()

    def measure_ndcg_deprecated(self, rec_list, test_pos):
        index = np.arange(len(rec_list))
        k = min(len(rec_list), len(test_pos))
        idcg = (1/np.log2(2+np.arange(k))).sum()
        dcg = (1/np.log2(2+index[np.isin(rec_list, test_pos)])).sum()

        return dcg/idcg

    def measure_ndcg(self, rec_list, test_pos, method=1):
        r = np.asfarray(np.isin(rec_list, test_pos))
        max_r = np.asfarray(sorted(np.isin(rec_list, test_pos), reverse=True))

        # if r.size:
        # dcg
        if method == 0:
            dcg = r[0] + np.sum(r[1:] / np.log2(np.arange(2, r.size + 1)))
        elif method == 1:
            dcg = np.sum(r / np.log2(np.arange(2, r.size + 2)))
        elif method == 2:
            return np.sum(r / np.log2(np.arange(2, r.size + 2)))

        # idcg
        if method == 0:
            idcg = max_r[0] + np.sum(max_r[1:] / np.log2(np.arange(2, max_r.size + 1)))
        elif method == 1:
            idcg = np.sum(max_r / np.log2(np.arange(2, max_r.size + 2)))
        if not idcg:
            return 0.
        else:
            return dcg / idcg

    def measure_hit_ratio(self, rec_list, test_pos):
        hit_count = np.isin(rec_list, test_pos).sum()

        if hit_count > 0:
            return 1.0
        else:
            return 0.0

    def measure_precision(self, rec_list, test_pos):
        hit_count = np.isin(rec_list, test_pos).sum()

        return hit_count / len(rec_list)

    def measure_average_precision(self, rec_list, test_pos, method=0):
        r = np.isin(rec_list, test_pos)
        out = [self.measure_precision(rec_list[:k+1], test_pos) for k in range(len(rec_list)) if r[k]]

        if not out:
            return 0.

        if method==0:
            return np.mean(out)
        elif method==1:
            return sum(out)/min(len(rec_list), len(test_pos))
        elif method==2:
            return sum(out)/len(test_pos)


    def measure_f1(self, rec_list, test_pos):
        recall = self.measure_recall(rec_list, test_pos)
        precision = self.measure_precision(rec_list, test_pos)
        if recall+precision == 0:
            value = 0
        else:
            value = 2*(recall*precision) / (recall+precision)

        return value

    def measure_mrr(self, rec_list, test_pos, method=0):

        r = np.asfarray(np.isin(rec_list, test_pos))

        if method == 0:
            rr = np.sum(r / np.arange(1, r.size + 1))   
        elif method == 1:
            r_ = 0
            for rank, i in enumerate(r):
                if i == 1:
                    r_ = rank + 1
                    break
            if r_ == 0:
                rr = 0
            else:
                rr = 1/r_

        return rr




    # # For user-fairness algorithm [WWW'21]
    # def ufair_ranking(self, user, train_item_set, train_pos, test_pos, train_embeddings, K, num_neg_samples=-1):

    #     new_item_set = list(set(train_item_set) - set(train_pos[user]) - set(test_pos[user]))
    #     if num_neg_samples != -1:
    #         neg_samples = random.sample(new_item_set, num_neg_samples)
    #     else:
    #         neg_samples = new_item_set

    #     relevances = {}
    #     cnt = 0

    #     for item in test_pos[user]:
    #         if item in train_item_set:
    #             relevances[item] = [train_embeddings[0][user] @ train_embeddings[1][item], 1.0]
    #         else:
    #             cnt += 1
    #             # new item in the test set (which does not have its embedding) -> give penalty (we cannot predict it)
    #             relevances[item] = [-100, 1.0]
    #             # if user==2929:
    #             #   print('new item@@@@@: user id: {}'.format(user))

    #     for item in neg_samples:
    #         if item in train_item_set:
    #             relevances[item] = [train_embeddings[0][user] @ train_embeddings[1][item], 0.0]
    #         else:
    #             print('@@@@@@@@error-test_pos@@@@@@@@@')

    #     # if user==2929:
    #     #   print(relevances)

    #     # sorted_relevances = sorted(relevances.items(), key=lambda x: x[1], reverse=True)
    #     # if K > 0:
    #     #   recommendation_list = [rel[0] for rel in sorted_relevances][:K]
    #     # else:
    #     #   recommendation_list = [rel[0] for rel in sorted_relevances]

    #     return relevances

    # # For user-fairness algorithm [WWW'21]
    # def data_preprocessing_for_ufair_algorithm(self, train_file, test_file, train_embeddings, num_neg_samples, file_adv, file_disadv, file_rank):
    #     self.init_results()
    #     train_edges = utils.read_data_from_file_int(train_file)
    #     test_edges = utilsread_data_from_file_int(test_file)

    #     train_pos = utilsget_user_dil_from_edgelist(train_edges)
    #     test_pos = utilsget_user_dil_from_edgelist(test_edges)

    #     train_user_set, train_item_set = utilsget_user_item_set(train_edges)
    #     test_user_set, test_item_set = utilsget_user_item_set(test_edges)

    #     with open(file_adv, 'w+') as f1, open(file_disadv, 'w+') as f2, open(file_rank, 'w+') as f3:
    #         f1.writelines('uid\tiid\tlabel\n')
    #         f2.writelines('uid\tiid\tlabel\n')
    #         f3.writelines('uid\tiid\tscore\tlabel\n')
    #         random.seed(10)
    #         for user in train_user_set:
    #             # Skip if the user is not in the test set
    #             if user in test_pos.keys():

    #                 for item in test_pos[user]:
    #                     if self.user_attr[user][0] == 0:
    #                         f1.writelines('{}\t{}\t1\n'.format(user, item))
    #                     elif self.user_attr[user][0] == 1:
    #                         f2.writelines('{}\t{}\t1\n'.format(user, item))

    #                 recommendation_list = self.ufair_ranking(user, train_item_set, train_pos, test_pos, train_embeddings, -1, num_neg_samples)
    #                 for item, scores in recommendation_list.items():
    #                     f3.writelines('{}\t{}\t{}\t{}\n'.format(user, item, scores[0], scores[1]))







# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import

import argparse
import csv
import logging
import os
import random
import sys
from io import open
import pandas as pd
import numpy as np
import torch
import gc
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler,
                              TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
from sklearn.metrics import f1_score
import json
from pytorch_transformers.modeling_bert import BertForSequenceClassification, BertConfig
from pytorch_transformers import AdamW, WarmupLinearSchedule
from pytorch_transformers.tokenization_bert import BertTokenizer
from itertools import cycle

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
MODEL_CLASSES = {
    'bert': (BertConfig, BertForSequenceClassification, BertTokenizer),
}
ALL_MODELS = sum((tuple(conf.pretrained_config_archive_map.keys()) for conf in (BertConfig,)), ())

logger = logging.getLogger(__name__)

label_map = {'财经': 0, '教育': 1, '房产': 2, '娱乐': 3, '游戏': 4,
             '体育': 5, '时尚': 6, '科技': 7, '时政': 8, '家居': 9}
label_map_rev = {v: k for k, v in label_map.items()}

class InputExample(object):
    """A single training/test example for simple sequence classification."""

    def __init__(self, guid, text_a, text_b=None, label=None):
        """Constructs a InputExample.
        Args:
            guid: Unique id for the example.
            text_a: string. The untokenized text of the first sequence. For single
            sequence tasks, only this sequence must be specified.
            text_b: (Optional) string. The untokenized text of the second sequence.
            Only must be specified for sequence pair tasks.
            label: (Optional) string. The label of the example. This should be
            specified for train and dev examples, but not for test examples.
        """
        self.guid = guid
        self.text_a = text_a
        self.text_b = text_b
        self.label = label


class InputFeatures(object):
    def __init__(self,
                 example_id,
                 choices_features,
                 label

                 ):
        self.example_id = example_id
        self.choices_features = [
            {
                'input_ids': input_ids,
                'input_mask': input_mask,
                'segment_ids': segment_ids
            }
            for _, input_ids, input_mask, segment_ids in choices_features
        ]
        self.label = label


def read_examples(input_file, is_training):
    df = pd.read_csv(input_file)
    examples = []
    for val in df[['id', 'content', 'label']].values:
        examples.append(InputExample(guid=val[0], text_a=val[1], label=val[2]))
    return examples


def convert_examples_to_features(examples, tokenizer, max_seq_length, split_num,
                                 is_training):
    """Loads a data file into a list of `InputBatch`s."""

    # Swag is a multiple choice task. To perform this task using Bert,
    # we will use the formatting proposed in "Improving Language
    # Understanding by Generative Pre-Training" and suggested by
    # @jacobdevlin-google in this issue
    # https://github.com/google-research/bert/issues/38.
    #
    # Each choice will correspond to a sample on which we run the
    # inference. For a given Swag example, we will create the 4
    # following inputs:
    # - [CLS] context [SEP] choice_1 [SEP]
    # - [CLS] context [SEP] choice_2 [SEP]
    # - [CLS] context [SEP] choice_3 [SEP]
    # - [CLS] context [SEP] choice_4 [SEP]
    # The model will output a single value for each input. To get the
    # final decision of the model, we will run a softmax over these 4
    # outputs.
    features = []
    for example_index, example in tqdm(enumerate(examples), desc="converting {} examples to features:".format(
            'train' if is_training else 'test'), total=len(examples)):

        context_tokens = tokenizer.tokenize(example.text_a)
        ending_tokens = tokenizer.tokenize(example.text_b)

        skip_len = len(context_tokens) / split_num
        choices_features = []
        for i in range(split_num):
            context_tokens_choice = context_tokens[int(i * skip_len):int((i + 1) * skip_len)]
            _truncate_seq_pair(context_tokens_choice, ending_tokens, max_seq_length - 2)
            tokens = ["[CLS]"] + context_tokens_choice + ["[SEP]"]
            segment_ids = [1] * (len(context_tokens_choice) + 2)
            input_ids = tokenizer.convert_tokens_to_ids(tokens)
            input_mask = [1] * len(input_ids)

            padding_length = max_seq_length - len(input_ids)
            input_ids += ([0] * padding_length)
            input_mask += ([0] * padding_length)
            segment_ids += ([0] * padding_length)
            choices_features.append((tokens, input_ids, input_mask, segment_ids))

            label = example.label
            if example_index < 1 and is_training:
                logger.info("*** Example ***")
                logger.info("idx: {}".format(example_index))
                logger.info("guid: {}".format(example.guid))
                logger.info("tokens: {}".format(' '.join(tokens).replace('\u2581', '_')))
                logger.info("input_ids: {}".format(' '.join(map(str, input_ids))))
                logger.info("input_mask: {}".format(' '.join(map(str, input_mask))))
                logger.info("segment_ids: {}".format(' '.join(map(str, segment_ids))))
                logger.info("label: {}".format(label))

        features.append(
            InputFeatures(
                example_id=example.guid,
                choices_features=choices_features,
                label=label
            )
        )
    return features


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    # 保证tokens_a+tokens_b的总长度小于等于max_length
    # 如果不满足，把比较长的那个list的最后一个元素删了，然后循环，直到满足为止
    # 当一个句子很短时，这样做与对每个句子删掉相同比例的token要更make sense，
    # 因为短句子中的token信息量应该会比长句子更大
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def accuracy(out, labels):
    outputs = np.argmax(out, axis=1)
    return f1_score(labels, outputs, labels=[0, 1, 2], average='macro')


def select_field(features, field):
    return [
        [
            choice[field]
            for choice in feature.choices_features
        ]
        for feature in features
    ]


def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir", default=None, type=str, required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type selected in the list: " + ", ".join(MODEL_CLASSES.keys()))
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(
                            ALL_MODELS))
    parser.add_argument("--meta_path", default=None, type=str, required=False,
                        help="Path to pre-trained model or shortcut name selected in the list: " + ", ".join(
                            ALL_MODELS))
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")
    parser.add_argument("--cache_dir", default="", type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length", default=128, type=int,
                        help="The maximum total input sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_test", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--evaluate_during_training", action='store_true',
                        help="Rul evaluation during training at each logging step.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")

    parser.add_argument("--per_gpu_train_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--per_gpu_eval_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--eval_steps", default=-1, type=int,
                        help="")
    parser.add_argument("--lstm_hidden_size", default=300, type=int,
                        help="")
    parser.add_argument("--lstm_layers", default=2, type=int,
                        help="")
    parser.add_argument("--lstm_dropout", default=0.5, type=float,
                        help="")

    parser.add_argument("--train_steps", default=-1, type=int,
                        help="")
    parser.add_argument("--report_steps", default=-1, type=int,
                        help="")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--split_num", default=3, type=int,
                        help="text split")
    parser.add_argument('--logging_steps', type=int, default=50,
                        help="Log every X updates steps.")
    parser.add_argument('--save_steps', type=int, default=50,
                        help="Save checkpoint every X updates steps.")
    parser.add_argument("--eval_all_checkpoints", action='store_true',
                        help="Evaluate all checkpoints starting with the same prefix as model_name ending and ending with step number")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")
    parser.add_argument('--overwrite_output_dir', action='store_true',
                        help="Overwrite the content of the output directory")
    parser.add_argument('--overwrite_cache', action='store_true',
                        help="Overwrite the cached training and evaluation sets")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")

    parser.add_argument('--fp16', action='store_true',
                        help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument('--fp16_opt_level', type=str, default='O1',
                        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
                             "See details at https://nvidia.github.io/apex/amp.html")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--server_ip', type=str, default='', help="For distant debugging.")
    parser.add_argument('--server_port', type=str, default='', help="For distant debugging.")
    parser.add_argument("--require_improvement", default=1000, type=int, help="若超过1000batch效果还没提升，则提前结束训练")

    args = parser.parse_args()

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
                   args.local_rank, device, args.n_gpu, bool(args.local_rank != -1), args.fp16)

    # Set seed
    set_seed(args)

    try:
        os.makedirs(args.output_dir)
    except:
        pass

    tokenizer = BertTokenizer.from_pretrained(args.model_name_or_path, do_lower_case=args.do_lower_case)

    config = BertConfig.from_pretrained(args.model_name_or_path, num_labels=10)

    # Prepare model
    model = BertForSequenceClassification.from_pretrained(args.model_name_or_path, args, config=config)

    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")
        model = DDP(model)
    elif args.n_gpu > 1:
        model = torch.nn.DataParallel(model)
    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    args.eval_batch_size = args.per_gpu_eval_batch_size * max(1, args.n_gpu)
    if args.do_train:

        # Prepare data loader
        train_examples = read_examples(os.path.join(args.data_dir, 'train.csv'), is_training=True)

        train_features = convert_examples_to_features(
                train_examples, tokenizer, args.max_seq_length, args.split_num, True)


        all_input_ids = torch.tensor(select_field(train_features, 'input_ids'), dtype=torch.long)
        all_input_mask = torch.tensor(select_field(train_features, 'input_mask'), dtype=torch.long)
        all_segment_ids = torch.tensor(select_field(train_features, 'segment_ids'), dtype=torch.long)
        all_label = torch.tensor([f.label for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label)
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler,
                                          batch_size=args.train_batch_size // args.gradient_accumulation_steps)

        num_train_optimization_steps = args.train_steps

        # Prepare optimizer

        param_optimizer = list(model.named_parameters())

        # hack to remove pooler, which is not used
        # thus it produce None grad that break apex
        param_optimizer = [n for n in param_optimizer]

        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)],
             'weight_decay': args.weight_decay},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
        scheduler = WarmupLinearSchedule(optimizer, warmup_steps=args.warmup_steps, t_total=args.train_steps)

        global_step = 0

        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)

        best_acc = 0
        model.train()
        tr_loss = 0
        nb_tr_examples, nb_tr_steps = 0, 0

        train_dataloader = cycle(train_dataloader)
        bar = tqdm(range(num_train_optimization_steps), total=num_train_optimization_steps)
        best_step = 0
        for step in bar:
            batch = next(train_dataloader)
            batch = tuple(t.to(device) for t in batch)
            input_ids, input_mask, segment_ids, label_ids = batch
            loss = model(input_ids=input_ids, token_type_ids=segment_ids, attention_mask=input_mask, labels=label_ids)
            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu.
            if args.fp16 and args.loss_scale != 1.0:
                loss = loss * args.loss_scale
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            tr_loss += loss.item()
            train_loss = round(tr_loss * args.gradient_accumulation_steps / (nb_tr_steps + 1), 4)
            bar.set_description("loss {}".format(train_loss))
            nb_tr_examples += input_ids.size(0)
            nb_tr_steps += 1

            if args.fp16:
                optimizer.backward(loss)
            else:
                loss.backward()

            if (nb_tr_steps + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    # modify learning rate with special warm up BERT uses
                    # if args.fp16 is False, BertAdam is used that handles this automatically
                    lr_this_step = args.learning_rate * warmup_linear.get_lr(global_step, args.warmup_proportion)
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr_this_step
                scheduler.step()
                optimizer.step()
                optimizer.zero_grad()
                global_step += 1

            if (step + 1) % (args.eval_steps * args.gradient_accumulation_steps) == 0:
                tr_loss = 0
                nb_tr_examples, nb_tr_steps = 0, 0
                logger.info("***** Report result *****")
                logger.info("  %s = %s", 'global_step', str(global_step))
                logger.info("  %s = %s", 'train loss', str(train_loss))

            if args.do_eval and (step + 1) % (args.eval_steps * args.gradient_accumulation_steps) == 0:
                file ='dev.csv'
                inference_labels = []
                gold_labels = []
                inference_logits = []
                # eval_features_path = './data/eval_features.pt'
                eval_examples = read_examples(os.path.join(args.data_dir, file), is_training=True)
                eval_features = convert_examples_to_features(eval_examples, tokenizer, args.max_seq_length,
                                                                 args.split_num, False)

                all_input_ids = torch.tensor(select_field(eval_features, 'input_ids'), dtype=torch.long)
                all_input_mask = torch.tensor(select_field(eval_features, 'input_mask'), dtype=torch.long)
                all_segment_ids = torch.tensor(select_field(eval_features, 'segment_ids'), dtype=torch.long)
                all_label = torch.tensor([f.label for f in eval_features], dtype=torch.long)

                eval_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label)

                logger.info("***** Running evaluation *****")
                logger.info("  Num examples = %d", len(eval_examples))
                logger.info("  Batch size = %d", args.eval_batch_size)

                # Run prediction for full data
                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

                model.eval()
                eval_loss, eval_accuracy = 0, 0
                nb_eval_steps, nb_eval_examples = 0, 0
                for input_ids, input_mask, segment_ids, label_ids in eval_dataloader:
                    input_ids = input_ids.to(device)
                    input_mask = input_mask.to(device)
                    segment_ids = segment_ids.to(device)
                    label_ids = label_ids.to(device)

                    with torch.no_grad():
                        tmp_eval_loss = model(input_ids=input_ids, token_type_ids=segment_ids,
                                              attention_mask=input_mask, labels=label_ids)
                        logits = model(input_ids=input_ids, token_type_ids=segment_ids, attention_mask=input_mask)

                    logits = logits.detach().cpu().numpy()
                    label_ids = label_ids.to('cpu').numpy()
                    inference_labels.append(np.argmax(logits, axis=1))
                    gold_labels.append(label_ids)
                    inference_logits.append(logits)
                    eval_loss += tmp_eval_loss.mean().item()
                    nb_eval_examples += input_ids.size(0)
                    nb_eval_steps += 1

                gold_labels = np.concatenate(gold_labels, 0)
                inference_logits = np.concatenate(inference_logits, 0)
                model.train()
                eval_loss = eval_loss / nb_eval_steps
                eval_accuracy = accuracy(inference_logits, gold_labels)

                result = {'eval_loss': eval_loss,
                          'eval_F1': eval_accuracy,
                          'global_step': global_step,
                          'loss': train_loss}

                output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
                with open(output_eval_file, "a") as writer:
                    for key in sorted(result.keys()):
                        logger.info("  %s = %s", key, str(result[key]))
                        writer.write("%s = %s\n" % (key, str(result[key])))
                    writer.write('*' * 80)
                    writer.write('\n')
                if eval_accuracy > best_acc and 'dev' in file:
                    print("=" * 80)
                    print("Best F1", eval_accuracy)
                    print("Saving Model......")
                    best_acc = eval_accuracy
                    # Save a trained model
                    model_to_save = model.module if hasattr(model,
                                                            'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
                    torch.save(model_to_save.state_dict(), output_model_file)
                    best_step = step
                    print("=" * 80)
                else:
                    print("=" * 80)

            if step - best_step > args.require_improvement:
                # 验证集loss超过1000batch没下降，结束训练
                print("No optimization for a long time, auto-stopping...")
                flag = True
                break
    if args.do_test:
        del model
        gc.collect()
        args.do_train = False
        model = BertForSequenceClassification.from_pretrained(os.path.join(args.output_dir, "pytorch_model.bin"), args,
                                                              config=config)
        if args.fp16:
            model.half()
        model.to(device)
        if args.local_rank != -1:
            try:
                from apex.parallel import DistributedDataParallel as DDP
            except ImportError:
                raise ImportError(
                    "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

            model = DDP(model)
        elif args.n_gpu > 1:
            model = torch.nn.DataParallel(model)

        for file, flag in [('test.csv', 'test')]:
            inference_labels = []
            gold_labels = []
            test_features_path = './data/test_features.pt'
            test_examples = read_examples(os.path.join(args.data_dir, file), is_training=False)
            if not os.path.exists(test_features_path):
                test_features = convert_examples_to_features(test_examples, tokenizer, args.max_seq_length, args.split_num,
                                                             False)
                torch.save(test_features,test_features_path)
                logger.info(f"{test_features_path} saved")

            else:
                test_features = torch.load(test_features_path)
            all_input_ids = torch.tensor(select_field(test_features, 'input_ids'), dtype=torch.long)
            all_input_mask = torch.tensor(select_field(test_features, 'input_mask'), dtype=torch.long)
            all_segment_ids = torch.tensor(select_field(test_features, 'segment_ids'), dtype=torch.long)
            all_label = torch.tensor([f.label for f in test_features], dtype=torch.long)

            test_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label)
            # Run prediction for full data
            test_sampler = SequentialSampler(test_data)
            test_dataloader = DataLoader(test_data, sampler=test_sampler, batch_size=args.eval_batch_size)

            model.eval()
            eval_loss, eval_accuracy = 0, 0
            nb_eval_steps, nb_eval_examples = 0, 0
            for input_ids, input_mask, segment_ids, label_ids in tqdm(test_dataloader,desc='test dataloader ',total=len(test_dataloader)):
                input_ids = input_ids.to(device)
                input_mask = input_mask.to(device)
                segment_ids = segment_ids.to(device)
                label_ids = label_ids.to(device)

                with torch.no_grad():
                    logits = model(input_ids=input_ids, token_type_ids=segment_ids,
                                   attention_mask=input_mask).detach().cpu().numpy()
                # label_ids = label_ids.to('cpu').numpy()
                inference_labels.append(logits)
                # gold_labels.append(label_ids)
            # gold_labels = np.concatenate(gold_labels, 0)
            logits = np.concatenate(inference_labels, 0)

            np.save(os.path.join(args.output_dir, "logits.npy"),logits)

            # if flag == 'test':
            #     df = pd.read_csv(os.path.join(args.data_dir, file))
            #     df['label_0'] = logits[:, 0]
            #     df['label_1'] = logits[:, 1]
            #     df['label_2'] = logits[:, 2]
            #     df[['id', 'label_0', 'label_1', 'label_2']].to_csv(os.path.join(args.output_dir, "sub.csv"),
            #                                                        index=False)

def generate_sub_file(config,test_data):
    test_data.columns = ["sub_content", "label", "len", "mask", "id"]
    test_data.drop(['sub_content', 'label', 'mask', 'len'], axis=1, inplace=True)
    print('getting npy file from '+config.npy_file_path)
    preds = np.load(config.npy_file_path)
    pre_df = pd.DataFrame(preds)
    sum_df = pd.concat([test_data, pre_df], axis=1).groupby('id').sum()
    preds = np.argmax(sum_df.values, axis=1)
    result = [label_map_rev[i] for i in list(preds)]
    sub = pd.concat([pd.read_csv(config.test_path,usecols=['id']), pd.DataFrame(result)], axis=1)
    sub.columns = ['id', 'class_label']
    sub['rank_label'] = sub['class_label'].apply(lambda x: classify_level(x))
    sub.to_csv(config.sub_path, index=False)
    print("submit file create at {}!".format(config.sub_path))


def classify_level(x):
    if x in ["财经", "时政"]:
        return "高风险"
    if x in ["房产", "科技"]:
        return "中风险"
    if x in ["教育", "时尚", "游戏"]:
        return "低风险"
    if x in ["家居", "体育", "娱乐"]:
        return "可公开"
    if x in ["未知"]:
        return "可公开"
    raise ValueError("类别不对")
if __name__ == "__main__":
    main()
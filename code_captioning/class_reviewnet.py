from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import ipdb
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import *

from classLSTMCore import LSTMCore
from classLSTMSoftAttentionCore import LSTMSoftAttentionCore
from classLSTMSoftAttentionNoInputCore import LSTMSoftAttentionNoInputCore


class ReviewNet(nn.Module):
    def __init__(self, opt):
        super(ReviewNet, self).__init__()

        self.token_cnt = opt.token_cnt
        self.word_cnt = opt.word_cnt
        self.lstm_size = opt.lstm_size
        self.drop_prob = opt.drop_prob

        self.input_encoding_size = opt.input_encoding_size
        self.encode_time_step = opt.code_truncate
        self.decode_time_step = opt.comment_truncate
        self.ss_prob = opt.ss_prob

        self.encoding_feat_size = opt.lstm_size
        self.encoding_att_size = opt.encoding_att_size
        self.att_hidden_size = opt.att_hidden_size
        self.num_review_steps = opt.num_review_steps
        self.drop_prob_reason = opt.drop_prob_reason

        # encoder
        self.encode_lstm = LSTMCore(self.input_encoding_size, self.lstm_size, self.drop_prob)

        # reviewer
        self.review_steps = nn.ModuleList([LSTMSoftAttentionNoInputCore(self.input_encoding_size,
                                                                        self.lstm_size,
                                                                        self.encoding_feat_size,
                                                                        self.encoding_att_size,
                                                                        self.att_hidden_size,
                                                                        self.drop_prob_reason)
                                           for _ in range(self.num_review_steps)])

        # decoder
        self.decode_lstm = LSTMSoftAttentionCore(self.input_encoding_size,
                                                 self.lstm_size,
                                                 self.encoding_feat_size,
                                                 self.num_review_steps,
                                                 self.att_hidden_size,
                                                 self.drop_prob)

        self.embed = nn.Embedding(self.token_cnt + 1, self.input_encoding_size)
        self.logit = nn.Linear(self.lstm_size, self.word_cnt)
        self.init_weights()

    def init_weights(self):
        self.embed.weight.data.uniform_(-0.1, 0.1)
        self.logit.weight.data.uniform_(-0.1, 0.1)
        self.logit.bias.data.fill_(0)

    def init_hidden(self, batch_size):
        weight = next(self.parameters()).data
        init_h = Variable(weight.new(1, batch_size, self.lstm_size).zero_())
        init_c = Variable(weight.new(1, batch_size, self.lstm_size).zero_())
        init_state = (init_h, init_c)
        return init_state

    def forward(self, code_matrix, comment_matrix, current_comment_mask_cuda):
        batch_size = code_matrix.size(0)
        encode_state = self.init_hidden(batch_size)
        decode_logit_seq = []
        outputs = []

        # encoder
        encode_hidden_states = []
        for i in range(self.encode_time_step):
            encode_words = code_matrix[:, i].clone()
            encode_xt = self.embed(encode_words)
            encode_output, encode_state = self.encode_lstm.forward(encode_xt, encode_state)
            encode_hidden_states.append(encode_output)
        encode_hidden_states = torch.cat([_.unsqueeze(1) for _ in encode_hidden_states], 1)  # batch x 300 x 512

        # reviewer
        review_state = (encode_state[0].clone(), encode_state[1].clone())
        thought = []
        for i in range(self.num_review_steps):
            review_output, review_state = self.review_steps[i].forward(encode_hidden_states, review_state)
            thought.append(review_output)
        thought_vectors = torch.stack(thought).transpose(0, 1).cuda().contiguous()  # thoughts vectors

        # decoder
        decode_state = (encode_state[0].clone(), encode_state[1].clone())
        for i in range(self.decode_time_step):
            if i >= 1 and self.ss_prob > 0.0:
                sample_prob = current_comment_mask_cuda.data.new(batch_size).uniform_(0, 1)
                sample_mask = sample_prob < self.ss_prob
                if sample_mask.sum() == 0:
                    it = comment_matrix[:, i].clone()
                else:
                    sample_ind = sample_mask.nonzero().view(-1)
                    it = comment_matrix[:, i].data.clone()
                    prob_prev = torch.exp(outputs[-1].data)  # fetch prev distribution: shape Nx(M+1)
                    it.index_copy_(0, sample_ind, torch.multinomial(prob_prev, 1).view(-1).index_select(0, sample_ind))
                    it = Variable(it, requires_grad=False)
            else:
                it = comment_matrix[:, i].clone()

            if i >= 1 and comment_matrix[:, i].data.sum() == 0:
                break

            decode_xt = self.embed(it)
            decode_output, decode_state = self.decode_lstm.forward(decode_xt, thought_vectors, decode_state)

            decode_logit_words = F.log_softmax(self.logit(decode_output))
            decode_logit_seq.append(decode_logit_words)
            outputs.append(decode_logit_words)

        # aggregate
        decode_logit_seq = torch.cat([_.unsqueeze(1) for _ in decode_logit_seq], 1).contiguous()

        return decode_logit_seq

    def sample(self, code_matrix, init_index, eos_index):
        batch_size = code_matrix.size(0)
        encode_state = self.init_hidden(batch_size)

        seq = []
        seqLogprobs = []
        logprobs_all = []

        # encoder
        encode_hidden_states = []
        for i in range(self.encode_time_step):
            encode_words = code_matrix[:, i].clone()

            if code_matrix[:, i].data.sum() == 0:
                break
            encode_xt = self.embed(encode_words)
            encode_output, encode_state = self.encode_lstm.forward(encode_xt, encode_state)
            encode_hidden_states.append(encode_output)
        encode_hidden_states = torch.cat([_.unsqueeze(1) for _ in encode_hidden_states], 1)

        # reviewer
        review_state = (encode_state[0].clone(), encode_state[1].clone())
        thought = []
        for i in range(self.num_review_steps):
            review_output, review_state = self.review_steps[i].forward(encode_hidden_states, review_state)
            thought.append(review_output)
        thought_vectors = torch.stack(thought).transpose(0, 1).cuda().contiguous()  # thoughts vectors

        # decoder
        decode_state = (encode_state[0].clone(), encode_state[1].clone())
        for i in range(self.decode_time_step):
            if i == 0:
                it = code_matrix.data.new(batch_size).long().fill_(init_index)
                decode_xt = self.embed(Variable(it, requires_grad=False).cuda())
                decode_output, decode_state = self.decode_lstm.forward(decode_xt, thought_vectors, decode_state)
            else:
                max_logprobs, it = torch.max(logprobs.data, 1)
                it = it.view(-1).long()

                if it.sum() == eos_index:
                    break

                decode_xt = self.embed(Variable(it, requires_grad=False).cuda())
                decode_output, decode_state = self.decode_lstm.forward(decode_xt, thought_vectors, decode_state)

                seq.append(it)
                seqLogprobs.append(max_logprobs.view(-1))

            logprobs = F.log_softmax(self.logit(decode_output))
            logprobs_all.append(logprobs)

        # aggregate
        greedy_seq = torch.cat([_.unsqueeze(1) for _ in seq], 1).contiguous()
        greedy_seq_probs = torch.cat([_.unsqueeze(1) for _ in seqLogprobs], 1).contiguous()
        greedy_logprobs_all = torch.cat([_.unsqueeze(1) for _ in logprobs_all], 1).contiguous()

        return greedy_seq, greedy_seq_probs, greedy_logprobs_all

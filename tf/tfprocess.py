#!/usr/bin/env python3
#
#    This file is part of Leela Zero.
#    Copyright (C) 2017-2018 Gian-Carlo Pascutto
#
#    Leela Zero is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    Leela Zero is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with Leela Zero.  If not, see <http://www.gnu.org/licenses/>.

import numpy as np
import os
import random
import tensorflow as tf
import time
import bisect

VERSION = 2

def weight_variable(shape, name=None):
    """Xavier initialization"""
    stddev = np.sqrt(2.0 / (sum(shape)))
    initial = tf.truncated_normal(shape, stddev=stddev)
    weights = tf.Variable(initial, name=name)
    tf.add_to_collection(tf.GraphKeys.REGULARIZATION_LOSSES, weights)
    return weights

# Bias weights for layers not followed by BatchNorm
# We do not regularlize biases, so they are not
# added to the regularlizer collection
def bias_variable(shape, name=None):
    initial = tf.constant(0.0, shape=shape)
    return tf.Variable(initial, name=name)

def conv2d(x, W):
    return tf.nn.conv2d(x, W, data_format='NCHW',
                        strides=[1, 1, 1, 1], padding='SAME')

class TFProcess:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root_dir = os.path.join(self.cfg['training']['path'], self.cfg['name'])

        # Network structure
        self.RESIDUAL_FILTERS = self.cfg['model']['filters']
        self.RESIDUAL_BLOCKS = self.cfg['model']['residual_blocks']

        # For exporting
        self.weights = []

        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=0.90, allow_growth=True, visible_device_list="{}".format(self.cfg['gpu']))
        config = tf.ConfigProto(gpu_options=gpu_options)
        self.session = tf.Session(config=config)

        self.training = tf.placeholder(tf.bool)
        self.global_step = tf.Variable(0, name='global_step', trainable=False)
        self.learning_rate = tf.placeholder(tf.float32)

    def init(self, dataset, train_iterator, test_iterator):
        # TF variables
        self.handle = tf.placeholder(tf.string, shape=[])
        iterator = tf.data.Iterator.from_string_handle(
            self.handle, dataset.output_types, dataset.output_shapes)
        self.next_batch = iterator.get_next()
        self.train_handle = self.session.run(train_iterator.string_handle())
        self.test_handle = self.session.run(test_iterator.string_handle())
        self.init_net(self.next_batch)

    def init_net(self, next_batch):
        self.x = next_batch[0]  # tf.placeholder(tf.float32, [None, 112, 8*8])
        self.y_ = next_batch[1] # tf.placeholder(tf.float32, [None, 1858])
        self.z_ = next_batch[2] # tf.placeholder(tf.float32, [None, 1])
        self.batch_norm_count = 0
        self.y_conv, self.z_conv = self.construct_net(self.x)

        # Calculate loss on policy head
        cross_entropy = \
            tf.nn.softmax_cross_entropy_with_logits(labels=self.y_,
                                                    logits=self.y_conv)
        self.policy_loss = tf.reduce_mean(cross_entropy)

        # Loss on value head
        self.mse_loss = \
            tf.reduce_mean(tf.squared_difference(self.z_, self.z_conv))

        # Regularizer
        regularizer = tf.contrib.layers.l2_regularizer(scale=0.0001)
        reg_variables = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
        self.reg_term = \
            tf.contrib.layers.apply_regularization(regularizer, reg_variables)

        # For training from a (smaller) dataset of strong players, you will
        # want to reduce the factor in front of self.mse_loss here.
        pol_loss_w = self.cfg['training']['policy_loss_weight']
        val_loss_w = self.cfg['training']['value_loss_weight']
        loss = pol_loss_w * self.policy_loss + val_loss_w * self.mse_loss + self.reg_term

        # Set adaptive learning rate during training
        self.cfg['training']['lr_boundaries'].sort()
        self.lr = self.cfg['training']['lr_values'][0]

        # You need to change the learning rate here if you are training
        # from a self-play training set, for example start with 0.005 instead.
        opt_op = tf.train.MomentumOptimizer(
            learning_rate=self.learning_rate, momentum=0.9, use_nesterov=True)


        self.update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(self.update_ops):
            self.train_op = \
                opt_op.minimize(loss, global_step=self.global_step)

        correct_prediction = \
            tf.equal(tf.argmax(self.y_conv, 1), tf.argmax(self.y_, 1))
        correct_prediction = tf.cast(correct_prediction, tf.float32)
        self.accuracy = tf.reduce_mean(correct_prediction)

        self.avg_policy_loss = []
        self.avg_mse_loss = []
        self.avg_reg_term = []
        self.time_start = None
        self.last_steps = None

        # Summary part
        self.test_writer = tf.summary.FileWriter(
            os.path.join(os.getcwd(), "leelalogs/{}-test".format(self.cfg['name'])), self.session.graph)
        self.train_writer = tf.summary.FileWriter(
            os.path.join(os.getcwd(), "leelalogs/{}-train".format(self.cfg['name'])), self.session.graph)

        self.init = tf.global_variables_initializer()
        self.saver = tf.train.Saver()

        self.session.run(self.init)

    def replace_weights(self, new_weights):
        for e, weights in enumerate(self.weights):
            if weights.name.endswith('/batch_normalization/beta:0'):
                # Batch norm beta is written as bias before the batch normalization
                # in the weight file for backwards compatibility reasons.
                bias = tf.constant(new_weights[e], shape=weights.shape)
                # Weight file order: bias, means, variances
                var = tf.constant(new_weights[e + 2], shape=weights.shape)
                new_beta = tf.divide(bias, tf.sqrt(var + tf.constant(1e-5)))
                self.session.run(tf.assign(weights, new_beta))
            elif weights.shape.ndims == 4:
                # Rescale rule50 related weights as clients do not normalize the input.
                if e == 0:
                    num_inputs = 112
                    # 50 move rule is the 110th input, or 109 starting from 0.
                    rule50_input = 109
                    for i in range(len(new_weights[e])):
                        if (i%(num_inputs*9))//9 == rule50_input:
                            new_weights[e][i] = new_weights[e][i]*99

                # Convolution weights need a transpose
                #
                # TF (kYXInputOutput)
                # [filter_height, filter_width, in_channels, out_channels]
                #
                # Leela/cuDNN/Caffe (kOutputInputYX)
                # [output, input, filter_size, filter_size]
                s = weights.shape.as_list()
                shape = [s[i] for i in [3, 2, 0, 1]]
                new_weight = tf.constant(new_weights[e], shape=shape)
                self.session.run(weights.assign(tf.transpose(new_weight, [2, 3, 1, 0])))
            elif weights.shape.ndims == 2:
                # Fully connected layers are [in, out] in TF
                #
                # [out, in] in Leela
                #
                s = weights.shape.as_list()
                shape = [s[i] for i in [1, 0]]
                new_weight = tf.constant(new_weights[e], shape=shape)
                self.session.run(weights.assign(tf.transpose(new_weight, [1, 0])))
            else:
                # Biases, batchnorm etc
                new_weight = tf.constant(new_weights[e], shape=weights.shape)
                self.session.run(tf.assign(weights, new_weight))
        #This should result in identical file to the starting one
        #self.save_leelaz_weights('restored.txt')

    def restore(self, file):
        print("Restoring from {0}".format(file))
        self.saver.restore(self.session, file)

    def process_loop(self, batch_size, test_batches):
        # Get the initial steps value in case this is a resume from a step count
        # which is not a multiple of total_steps.
        steps = tf.train.global_step(self.session, self.global_step)
        total_steps = self.cfg['training']['total_steps']
        for _ in range(steps % total_steps, total_steps):
            self.process(batch_size, test_batches)

    def process(self, batch_size, test_batches):
        if not self.time_start:
            self.time_start = time.time()

        # Get the initial steps value before we do a training step.
        steps = tf.train.global_step(self.session, self.global_step)
        if not self.last_steps:
            self.last_steps = steps

        # Run test before first step to see delta since end of last run.
        if steps % self.cfg['training']['total_steps'] == 0:
            # Steps is given as one higher than current in order to avoid it
            # being equal to the value the end of a run is stored against.
            self.calculate_test_summaries(test_batches, steps + 1)

        # Run training for this batch
        policy_loss, mse_loss, reg_term, _, _ = self.session.run(
            [self.policy_loss, self.mse_loss, self.reg_term, self.train_op,
                self.next_batch],
            feed_dict={self.training: True, self.learning_rate: self.lr, self.handle: self.train_handle})

        # Update steps since training should have incremented it.
        steps = tf.train.global_step(self.session, self.global_step)

        # Determine learning rate
        lr_values = self.cfg['training']['lr_values']
        lr_boundaries = self.cfg['training']['lr_boundaries']
        steps_total = (steps-1) % self.cfg['training']['total_steps']
        self.lr = lr_values[bisect.bisect_right(lr_boundaries, steps_total)]

        # Keep running averages
        # Google's paper scales MSE by 1/4 to a [0, 1] range, so do the same to
        # get comparable values.
        mse_loss /= 4.0
        self.avg_policy_loss.append(policy_loss)
        self.avg_mse_loss.append(mse_loss)
        self.avg_reg_term.append(reg_term)
        if steps % self.cfg['training']['train_avg_report_steps'] == 0 or steps % self.cfg['training']['total_steps'] == 0:
            pol_loss_w = self.cfg['training']['policy_loss_weight']
            val_loss_w = self.cfg['training']['value_loss_weight']
            time_end = time.time()
            speed = 0
            if self.time_start:
                elapsed = time_end - self.time_start
                steps_elapsed = steps - self.last_steps
                speed = batch_size * (steps_elapsed / elapsed)
            avg_policy_loss = np.mean(self.avg_policy_loss or [0])
            avg_mse_loss = np.mean(self.avg_mse_loss or [0])
            avg_reg_term = np.mean(self.avg_reg_term or [0])
            print("step {}, lr={:g} policy={:g} mse={:g} reg={:g} total={:g} ({:g} pos/s)".format(
                steps, self.lr, avg_policy_loss, avg_mse_loss, avg_reg_term,
                # Scale mse_loss back to the original to reflect the actual
                # value being optimized.
                # If you changed the factor in the loss formula above, you need
                # to change it here as well for correct outputs.
                pol_loss_w * avg_policy_loss + val_loss_w * 4.0 * avg_mse_loss + avg_reg_term,
                speed))
            train_summaries = tf.Summary(value=[
                tf.Summary.Value(tag="Policy Loss", simple_value=avg_policy_loss),
                tf.Summary.Value(tag="Reg term", simple_value=avg_reg_term),
                tf.Summary.Value(tag="LR", simple_value=self.lr),
                tf.Summary.Value(tag="MSE Loss", simple_value=avg_mse_loss)])
            self.train_writer.add_summary(train_summaries, steps)
            self.time_start = time_end
            self.last_steps = steps
            self.avg_policy_loss, self.avg_mse_loss, self.avg_reg_term = [], [], []

        # Calculate test values every 'test_steps', but also ensure there is
        # one at the final step so the delta to the first step can be calculted.
        if steps % self.cfg['training']['test_steps'] == 0 or steps % self.cfg['training']['total_steps'] == 0:
            self.calculate_test_summaries(test_batches, steps)

        # Save session and weights at end, and also optionally every 'checkpoint_steps'.
        if steps % self.cfg['training']['total_steps'] == 0 or (
                'checkpoint_steps' in self.cfg['training'] and steps % self.cfg['training']['checkpoint_steps'] == 0):
            path = os.path.join(self.root_dir, self.cfg['name'])
            save_path = self.saver.save(self.session, path, global_step=steps)
            print("Model saved in file: {}".format(save_path))
            leela_path = path + "-" + str(steps) + ".txt"
            self.save_leelaz_weights(leela_path) 
            print("Weights saved in file: {}".format(leela_path))

    def calculate_test_summaries(self, test_batches, steps):
        sum_accuracy = 0
        sum_mse = 0
        sum_policy = 0
        for _ in range(0, test_batches):
            test_policy, test_accuracy, test_mse, _ = self.session.run(
                [self.policy_loss, self.accuracy, self.mse_loss,
                 self.next_batch],
                feed_dict={self.training: False,
                           self.handle: self.test_handle})
            sum_accuracy += test_accuracy
            sum_mse += test_mse
            sum_policy += test_policy
        sum_accuracy /= test_batches
        sum_accuracy *= 100
        sum_policy /= test_batches
        # Additionally rescale to [0, 1] so divide by 4
        sum_mse /= (4.0 * test_batches)
        test_summaries = tf.Summary(value=[
            tf.Summary.Value(tag="Accuracy", simple_value=sum_accuracy),
            tf.Summary.Value(tag="Policy Loss", simple_value=sum_policy),
            tf.Summary.Value(tag="MSE Loss", simple_value=sum_mse)]).SerializeToString()
        histograms = [tf.summary.histogram(weight.name, weight) for weight in self.weights]
        test_summaries = tf.summary.merge([test_summaries] + histograms).eval(session=self.session)
        self.test_writer.add_summary(test_summaries, steps)
        print("step {}, policy={:g} training accuracy={:g}%, mse={:g}".\
            format(steps, sum_policy, sum_accuracy, sum_mse))

    def save_leelaz_weights(self, filename):
        with open(filename, "w") as file:
            # Version tag
            file.write("{}".format(VERSION))
            for e, weights in enumerate(self.weights):
                # Newline unless last line (single bias)
                file.write("\n")
                work_weights = None
                if weights.name.endswith('/batch_normalization/beta:0'):
                    # Batch norm beta needs to be converted to biases before
                    # the batch norm for backwards compatibility reasons
                    var_key = weights.name.replace('beta', 'moving_variance')
                    var = tf.get_default_graph().get_tensor_by_name(var_key)
                    work_weights = tf.multiply(weights, tf.sqrt(var + tf.constant(1e-5)))
                elif weights.shape.ndims == 4:
                    # Convolution weights need a transpose
                    #
                    # TF (kYXInputOutput)
                    # [filter_height, filter_width, in_channels, out_channels]
                    #
                    # Leela/cuDNN/Caffe (kOutputInputYX)
                    # [output, input, filter_size, filter_size]
                    work_weights = tf.transpose(weights, [3, 2, 0, 1])
                elif weights.shape.ndims == 2:
                    # Fully connected layers are [in, out] in TF
                    #
                    # [out, in] in Leela
                    #
                    work_weights = tf.transpose(weights, [1, 0])
                else:
                    # Biases, batchnorm etc
                    work_weights = weights
                nparray = work_weights.eval(session=self.session)
                # Rescale rule50 related weights as clients do not normalize the input.
                if e == 0:
                    num_inputs = 112
                    # 50 move rule is the 110th input, or 109 starting from 0.
                    rule50_input = 109
                    wt_str = []
                    for i, weight in enumerate(np.ravel(nparray)):
                        if (i%(num_inputs*9))//9 == rule50_input:
                            wt_str.append(str(weight/99))
                        else:
                            wt_str.append(str(weight))
                else:
                    wt_str = [str(wt) for wt in np.ravel(nparray)]
                file.write(" ".join(wt_str))

    def get_batchnorm_key(self):
        result = "bn" + str(self.batch_norm_count)
        self.batch_norm_count += 1
        return result

    def conv_block(self, inputs, filter_size, input_channels, output_channels):
        # The weights are internal to the batchnorm layer, so apply
        # a unique scope that we can store, and use to look them back up
        # later on.
        weight_key = self.get_batchnorm_key()
        conv_key = weight_key + "/conv_weight"
        W_conv = weight_variable([filter_size, filter_size,
                                  input_channels, output_channels], name=conv_key)

        with tf.variable_scope(weight_key):
            h_bn = \
                tf.layers.batch_normalization(
                    conv2d(inputs, W_conv),
                    epsilon=1e-5, axis=1, fused=True,
                    center=True, scale=False,
                    training=self.training)
        h_conv = tf.nn.relu(h_bn)

        beta_key = weight_key + "/batch_normalization/beta:0"
        mean_key = weight_key + "/batch_normalization/moving_mean:0"
        var_key = weight_key + "/batch_normalization/moving_variance:0"

        beta = tf.get_default_graph().get_tensor_by_name(beta_key)
        mean = tf.get_default_graph().get_tensor_by_name(mean_key)
        var = tf.get_default_graph().get_tensor_by_name(var_key)

        self.weights.append(W_conv)
        self.weights.append(beta)
        self.weights.append(mean)
        self.weights.append(var)

        return h_conv

    def residual_block(self, inputs, channels):
        # First convnet
        orig = tf.identity(inputs)
        weight_key_1 = self.get_batchnorm_key()
        conv_key_1 = weight_key_1 + "/conv_weight"
        W_conv_1 = weight_variable([3, 3, channels, channels], name=conv_key_1)

        # Second convnet
        weight_key_2 = self.get_batchnorm_key()
        conv_key_2 = weight_key_2 + "/conv_weight"
        W_conv_2 = weight_variable([3, 3, channels, channels], name=conv_key_2)

        with tf.variable_scope(weight_key_1):
            h_bn1 = \
                tf.layers.batch_normalization(
                    conv2d(inputs, W_conv_1),
                    epsilon=1e-5, axis=1, fused=True,
                    center=True, scale=False,
                    training=self.training)
        h_out_1 = tf.nn.relu(h_bn1)
        with tf.variable_scope(weight_key_2):
            h_bn2 = \
                tf.layers.batch_normalization(
                    conv2d(h_out_1, W_conv_2),
                    epsilon=1e-5, axis=1, fused=True,
                    center=True, scale=False,
                    training=self.training)
        h_out_2 = tf.nn.relu(tf.add(h_bn2, orig))

        beta_key_1 = weight_key_1 + "/batch_normalization/beta:0"
        mean_key_1 = weight_key_1 + "/batch_normalization/moving_mean:0"
        var_key_1 = weight_key_1 + "/batch_normalization/moving_variance:0"

        beta_1 = tf.get_default_graph().get_tensor_by_name(beta_key_1)
        mean_1 = tf.get_default_graph().get_tensor_by_name(mean_key_1)
        var_1 = tf.get_default_graph().get_tensor_by_name(var_key_1)

        beta_key_2 = weight_key_2 + "/batch_normalization/beta:0"
        mean_key_2 = weight_key_2 + "/batch_normalization/moving_mean:0"
        var_key_2 = weight_key_2 + "/batch_normalization/moving_variance:0"

        beta_2 = tf.get_default_graph().get_tensor_by_name(beta_key_2)
        mean_2 = tf.get_default_graph().get_tensor_by_name(mean_key_2)
        var_2 = tf.get_default_graph().get_tensor_by_name(var_key_2)

        self.weights.append(W_conv_1)
        self.weights.append(beta_1)
        self.weights.append(mean_1)
        self.weights.append(var_1)

        self.weights.append(W_conv_2)
        self.weights.append(beta_2)
        self.weights.append(mean_2)
        self.weights.append(var_2)

        return h_out_2

    def construct_net(self, planes):
        # NCHW format
        # batch, 112 input channels, 8 x 8
        x_planes = tf.reshape(planes, [-1, 112, 8, 8])

        # Input convolution
        flow = self.conv_block(x_planes, filter_size=3,
                               input_channels=112,
                               output_channels=self.RESIDUAL_FILTERS)
        # Residual tower
        for _ in range(0, self.RESIDUAL_BLOCKS):
            flow = self.residual_block(flow, self.RESIDUAL_FILTERS)

        # Policy head
        conv_pol = self.conv_block(flow, filter_size=1,
                                   input_channels=self.RESIDUAL_FILTERS,
                                   output_channels=32)
        h_conv_pol_flat = tf.reshape(conv_pol, [-1, 32*8*8])
        W_fc1 = weight_variable([32*8*8, 1858], name='fc1/weight')
        b_fc1 = bias_variable([1858], name='fc1/bias')
        self.weights.append(W_fc1)
        self.weights.append(b_fc1)
        h_fc1 = tf.add(tf.matmul(h_conv_pol_flat, W_fc1), b_fc1, name='policy_head')

        # Value head
        conv_val = self.conv_block(flow, filter_size=1,
                                   input_channels=self.RESIDUAL_FILTERS,
                                   output_channels=32)
        h_conv_val_flat = tf.reshape(conv_val, [-1, 32*8*8])
        W_fc2 = weight_variable([32 * 8 * 8, 128], name='fc2/weight')
        b_fc2 = bias_variable([128], name='fc2/bias')
        self.weights.append(W_fc2)
        self.weights.append(b_fc2)
        h_fc2 = tf.nn.relu(tf.add(tf.matmul(h_conv_val_flat, W_fc2), b_fc2))
        W_fc3 = weight_variable([128, 1], name='fc3/weight')
        b_fc3 = bias_variable([1], name='fc3/bias')
        self.weights.append(W_fc3)
        self.weights.append(b_fc3)
        h_fc3 = tf.nn.tanh(tf.add(tf.matmul(h_fc2, W_fc3), b_fc3), name='value_head')

        return h_fc1, h_fc3

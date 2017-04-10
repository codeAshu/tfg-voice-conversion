# Created by albert aparicio on 31/03/17
# coding: utf-8

# This script defines an Sequence-to-Sequence model, using the implemented
# encoders and decoders from TensorFlow

# TODO Document and explain steps
# TODO Move this model to tfglib

# This import makes Python use 'print' as in Python 3.x
from __future__ import print_function

import os

import numpy as np
import tensorflow as tf
import tfglib.seq2seq_datatable as s2s
from tfglib.seq2seq_normalize import maxmin_scaling
from tfglib.utils import init_logger


class Seq2Seq(object):
  # TODO Figure out suitable values for attention length and size
  def __init__(self, logger_level, enc_rnn_layers, dec_rnn_layers, rnn_size,
               seq_length, params_length, cell_type='lstm', batch_size=20,
               learning_rate=0.001, dropout=0.5, optimizer='adam', clip_norm=5,
               attn_length=500, attn_size=100, infer=False):
    """
    infer: only True if used for test or predictions. False to train.
    """
    self.logger = init_logger(name=__name__, level=logger_level)

    self.logger.debug('Seq2Seq init')
    self.rnn_size = rnn_size
    self.attn_length = attn_length
    self.attn_size = attn_size
    # Number of layers in encoder and decoder
    self.enc_rnn_layers = enc_rnn_layers
    self.dec_rnn_layers = dec_rnn_layers
    self.infer = infer
    if infer:
      self.keep_prob = tf.Variable(1., trainable=False)
    else:
      self.keep_prob = tf.Variable((1. - dropout), trainable=False)

    self.dropout = dropout
    self.cell_type = cell_type
    self.batch_size = batch_size
    self.clip_norm = clip_norm
    self.learning_rate = learning_rate

    # TODO The sequence length should be the actual seq_length of each sequence?
    # self.seq_length = seq_length
    # self.seq_length = seq_length * np.ones((batch_size,))
    self.seq_length = tf.placeholder(tf.int32, [self.batch_size])

    self.parameters_length = params_length
    self.gtruth = tf.placeholder(tf.float32,
                                 [self.batch_size,
                                  seq_length,
                                  self.parameters_length])
    #
    # self.encoder_inputs = tf.placeholder(tf.float32,
    #                                      [batch_size,
    #                                       self.seq_length,
    #                                       self.parameters_length])
    # inputs: A length T list of inputs, each a Tensor of
    # shape [batch_size, input_size], or a nested tuple of such elements
    self.encoder_inputs = [
      tf.placeholder(tf.float32, [batch_size, self.parameters_length]
                     ) for _ in range(seq_length)]

    # self.decoder_inputs = tf.placeholder(tf.float32,
    #                                      [batch_size,
    #                                       seq_length,
    #                                       self.parameters_length])
    # decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    self.decoder_inputs = [
      tf.placeholder(tf.float32, [batch_size, self.parameters_length]
                     ) for _ in range(seq_length)]

    # # attention_states: 3D Tensor [batch_size x attn_length x attn_size]
    # self.attention_states = tf.placeholder(tf.float32,
    #                                        [batch_size,
    #                                         self.attn_length,
    #                                         self.attn_size])

    # To be assigned a value in self.inference()
    self.enc_state, self.encoder_vars = None, None
    self.prediction = self.inference()

    self.loss = self.mse_loss(self.gtruth, self.prediction)

    tvars = tf.trainable_variables()
    grads = []
    for grad in tf.gradients(self.loss, tvars):
      if grad is not None:
        grads.append(tf.clip_by_norm(grad, self.clip_norm))
      else:
        grads.append(grad)

    self.optimizer = optimizer
    # set up a variable to make the learning rate evolve during training
    self.curr_lr = tf.Variable(self.learning_rate, trainable=False)
    self.opt = tf.train.AdamOptimizer(self.curr_lr)

    self.train_op = self.opt.apply_gradients(zip(grads, tvars))

  def build_multirnn_block(self, rnn_size, rnn_layers, cell_type):
    self.logger.debug('Build RNN block')
    if cell_type == 'gru':
      cell = tf.contrib.rnn.GRUCell(rnn_size)
    elif cell_type == 'lstm':
      cell = tf.contrib.rnn.BasicLSTMCell(rnn_size, state_is_tuple=True)
    else:
      raise ValueError("The selected cell type '%s' is not supported"
                       % cell_type)

    if rnn_layers > 1:
      cell = tf.contrib.rnn.MultiRNNCell([cell] * rnn_layers,
                                         state_is_tuple=True)
    return cell

  def mse_loss(self, gtruth, prediction):
    # Previous to the computation, I gotta mask the predictions
    self.logger.debug('Compute loss')
    return tf.reduce_mean(tf.squared_difference(gtruth, prediction))

  def inference(self):
    self.logger.debug('Inference')
    from tensorflow.contrib.legacy_seq2seq.python.ops.seq2seq import \
      attention_decoder as tf_attention_decoder
    from tensorflow.contrib.rnn.python.ops.core_rnn import \
      static_rnn as tf_static_rnn

    self.logger.debug('Imported seq2seq model from TF')

    with tf.variable_scope("encoder"):
      enc_cell = self.build_multirnn_block(self.rnn_size,
                                           self.enc_rnn_layers,
                                           self.cell_type)
      self.enc_zero = enc_cell.zero_state(self.batch_size, tf.float32)

      self.logger.debug('Initialize encoder')
      enc_out, enc_state = tf_static_rnn(enc_cell,
                                         self.encoder_inputs,
                                         initial_state=self.enc_zero,
                                         sequence_length=self.seq_length)

    # this op is created to visualize the thought vectors
    self.enc_state = enc_state
    self.logger.info(
      'enc out (len {}) tensors shape: {}'.format(
        len(enc_out), enc_out[0].get_shape()
      ))
    # print('enc out tensor shape: ', enc_out.get_shape())

    dec_cell = self.build_multirnn_block(self.rnn_size,
                                         self.dec_rnn_layers,
                                         self.cell_type)
    if self.dropout > 0:
      # print('Applying dropout {} to decoder'.format(self.dropout))
      self.logger.info('Applying dropout {} to decoder'.format(self.dropout))
      dec_cell = tf.contrib.rnn.DropoutWrapper(dec_cell,
                                               input_keep_prob=self.keep_prob)

    dec_cell = tf.contrib.rnn.OutputProjectionWrapper(dec_cell,
                                                      self.parameters_length)
    if not self.infer:
      def loop_function(prev, _):
        return prev
    else:
      loop_function = None

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
      tf.reshape(e, [-1, 1, enc_cell.output_size]) for e in enc_out
    ]
    attention_states = tf.concat(top_states, 1)

    self.logger.debug('Initialize decoder')
    dec_out, dec_state = tf_attention_decoder(
      self.decoder_inputs, enc_state, cell=dec_cell,
      attention_states=attention_states, loop_function=loop_function
    )

    # print('dec_state shape: ', dec_state[0].get_shape())
    # merge outputs into a tensor and transpose to be [B, seq_length, out_dim]
    dec_outputs = tf.transpose(tf.stack(dec_out), [1, 0, 2])
    # print('dec outputs shape: ', dec_outputs.get_shape())
    self.logger.info('dec outputs shape: {}'.format(dec_outputs.get_shape()))

    self.encoder_vars = {}
    for tvar in tf.trainable_variables():
      if 'char_embedding' in tvar.name or 'encoder' in tvar.name:
        self.encoder_vars[tvar.name] = tvar
      print('tvar: ', tvar.name)

    return dec_outputs

  def save(self, sess, save_filename, global_step=None):
    if not hasattr(self, 'saver'):
      self.saver = tf.train.Saver()
    if not hasattr(self, 'encoder_saver'):
      self.encoder_saver = tf.train.Saver(var_list=self.encoder_vars)
    print('Saving checkpoint...')
    if global_step is not None:
      self.encoder_saver.save(sess, save_filename + '.encoder',
                              global_step)
      self.saver.save(sess, save_filename, global_step)
    else:
      self.encoder_saver.save(sess, save_filename + '.encoder')
      self.saver.save(sess, save_filename)

  def load(self, sess, save_path):
    if not hasattr(self, 'saver'):
      self.saver = tf.train.Saver()
    if os.path.exists(os.path.join(save_path, 'best_model.ckpt')):
      ckpt_name = os.path.join(save_path, 'best_model.ckpt')
      print('Loading checkpoint {}...'.format(ckpt_name))
      self.saver.restore(sess, os.path.join(save_path, ckpt_name))
    else:
      ckpt = tf.train.get_checkpoint_state(save_path)
      if ckpt and ckpt.model_checkpoint_path:
        ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
        print('Loading checkpoint {}...'.format(ckpt_name))
        self.saver.restore(sess, os.path.join(save_path, ckpt_name))
        return True
      else:
        return False


class DataLoader(object):
  # TODO Finish this class and move it to a new file
  def __init__(self, args, logger_level, max_seq_length=None):
    self.logger = init_logger(name=__name__, level=logger_level)

    self.logger.debug('DataLoader init')
    self.batch_size = args.batch_size

    (self.src_datatable,
     self.trg_datatable,
     self.trg_masks,
     self.train_spk,
     self.train_spk_max,
     self.train_spk_min,
     self.max_seq_length
     ) = self.load_dataset(
      args.train_data_path,
      args.train_out_file,
      args.save_h5,
      args.train_out_file,
      args.val_fraction
    )

    if max_seq_length is not None:
      self.max_seq_length = max_seq_length

    self.batches_per_epoch = int(
      np.floor(self.src_datatable.shape[0] / self.batch_size)
    )

  def load_dataset(self, data_path, out_file, save_h5, train_out_file,
                   validation_fraction):
    self.logger.debug('Load dataset')
    if save_h5:
      self.logger.info('Saving datatable')

      (src_datatable,
       src_masks,
       trg_datatable,
       trg_masks,
       max_seq_length,
       train_speakers_max,
       train_speakers_min
       ) = s2s.seq2seq_save_datatable(data_path, out_file)

      self.logger.info('DONE Saving datatable')

    else:
      self.logger.info('Load parameters. File: {}'.format(train_out_file))
      (src_datatable,
       src_masks,
       trg_datatable,
       trg_masks,
       max_seq_length,
       train_speakers_max,
       train_speakers_min
       ) = s2s.seq2seq2_load_datatable(train_out_file + '.h5')
      self.logger.info('DONE Loaded parameters')

    train_speakers = train_speakers_max.shape[0]

    # Normalize data
    self.logger.debug('Normalize data')
    # Iterate over sequence 'slices'
    assert src_datatable.shape[0] == trg_datatable.shape[0]

    for i in range(src_datatable.shape[0]):
      (
        src_datatable[i, :, 0:42],
        trg_datatable[i, :, 0:42]
      ) = maxmin_scaling(
        src_datatable[i, :, :],
        src_masks[i, :],
        trg_datatable[i, :, :],
        trg_masks[i, :],
        train_speakers_max,
        train_speakers_min
      )

    # # TODO Implement choice between returning training data or validation data
    # self.logger.debug('Split into training and validation')
    # ################################################
    # # Split data into training and validation sets #
    # ################################################
    # # #################################
    # # # TODO COMMENT AFTER DEVELOPING #
    # # #################################
    # # batch_size = 2
    # # nb_epochs = 2
    # #
    # # num = 10
    # # src_datatable = src_datatable[0:num]
    # # src_masks = src_masks[0:num]
    # # trg_datatable = trg_datatable[0:num]
    # # trg_masks = trg_masks[0:num]
    # #
    # # model_description = 'DEV_' + model_description
    # # #################################################
    #
    # src_data = src_datatable[0:int(np.floor(
    #   src_datatable.shape[0] * (1 - validation_fraction)))]
    # src_valid_data = src_datatable[int(np.floor(
    #   src_datatable.shape[0] * (1 - validation_fraction))):]
    #
    # trg_data = trg_datatable[0:int(np.floor(
    #   trg_datatable.shape[0] * (1 - validation_fraction)))]
    # trg_valid_data = trg_datatable[int(np.floor(
    #   trg_datatable.shape[0] * (1 - validation_fraction))):]
    #
    # trg_masks_f = trg_masks[0:int(np.floor(
    #   trg_masks.shape[0] * (1 - validation_fraction)))]
    # trg_valid_masks_f = trg_masks[int(np.floor(
    #   trg_masks.shape[0] * (1 - validation_fraction))):]
    #
    # return (src_data, src_valid_data, trg_data, trg_valid_data, trg_masks_f,
    #         trg_valid_masks_f, train_speakers, train_speakers_max,
    #         train_speakers_min, max_seq_length)

    return (src_datatable, trg_datatable, trg_masks, train_speakers,
            train_speakers_max, train_speakers_min, max_seq_length)

  def next_batch(self):
    self.logger.debug('Initialize next batch generator')
    batch_id = 0
    while True:
      self.logger.debug('--> Next batch - Prepare <--')
      src_batch = self.src_datatable[
                  batch_id * self.batch_size:(batch_id + 1) * self.batch_size,
                  :, :]
      trg_batch = self.trg_datatable[
                  batch_id * self.batch_size:(batch_id + 1) * self.batch_size,
                  :, :]
      trg_mask = self.trg_masks[
                 batch_id * self.batch_size:(batch_id + 1) * self.batch_size, :]

      batch_id = (batch_id + 1) % self.batches_per_epoch

      self.logger.debug('--> Next batch - Yield <--')
      yield (src_batch, trg_batch, trg_mask)

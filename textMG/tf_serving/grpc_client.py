"""A client that talks to tensorflow_model_server loaded with mnist model.
The client downloads test images of mnist data set, queries the service with
such test images to get predictions, and calculates the inference error rate.
Typical usage example:
    mnist_client.py --num_tests=100 --server=localhost:9000
"""
from __future__ import print_function

import os
import sys
sys.path.append(os.path.dirname(os.getcwd()))

import sys
import threading
import grpc
import numpy as np
from time import time
import tensorflow as tf
from textMG.configs.config import args, label_dict
from textMG.datasets.dataset import Dataset

from tensorflow_serving.apis import predict_pb2
from tensorflow_serving.apis import prediction_service_pb2_grpc

tf.compat.v1.app.flags.DEFINE_integer(
    'concurrency', 500, 'maximum number of concurrent inference requests')
tf.compat.v1.app.flags.DEFINE_integer('num_tests', 1000, 'Number of test images')
tf.compat.v1.app.flags.DEFINE_string('server', 'localhost:8500',
                                     'PredictionService host:port')
tf.compat.v1.app.flags.DEFINE_bool('print_outputs', False, 'whether or not to print output predictions')    
              
# tf.compat.v1.app.flags.DEFINE_string('work_dir', '/tmp', 'Working directory. ')
FLAGS = tf.compat.v1.app.flags.FLAGS

class _ResultCounter(object):
  """Counter for the prediction results."""

  def __init__(self, num_tests, concurrency):
    self._num_tests = num_tests
    self._concurrency = concurrency
    self._error = 0
    self.predictions = []
    self.model_version = None
    self._done = 0
    self._active = 0
    self._condition = threading.Condition()

  def inc_error(self):
    with self._condition:
      self._error += 1

  def inc_done(self):
    with self._condition:
      self._done += 1
      self._condition.notify()

  def dec_active(self):
    with self._condition:
      self._active -= 1
      self._condition.notify()

  def get_predictions(self):
    with self._condition:
      while self._done != self._num_tests:
        self._condition.wait()
      return self.predictions
  def get_model_version(self):
    with self._condition:
      while self._done != self._num_tests:
        self._condition.wait()
      return self.model_version
  
  def throttle(self):
    with self._condition:
      while self._active == self._concurrency:
        self._condition.wait()
      self._active += 1

def _create_rpc_callback(result_counter):
  """Creates RPC callback function.
  Args:
    label: The correct label for the predicted example.
    result_counter: Counter for the prediction result.
  Returns:
    The callback function.
  """
  def _callback(result_future):
    """Callback function.
    Calculates the statistics for the prediction result.
    Args:
      result_future: Result future of the RPC.
    """
    exception = result_future.exception()
    if exception:
      result_counter.inc_error()
      print(exception)
    else:
      # sys.stdout.write('.')
      sys.stdout.flush()
      # response = np.array(
      #     result_future.result().outputs['scores'].float_val)
      # prediction = np.argmax(response)
      # if label != prediction:
      #   result_counter.inc_error()
      response = result_future.result().outputs['prediction'].int64_val
      res = [key for key, value in label_dict.items() if value == response[0]]
      result_counter.predictions.append(res[0])
      if result_counter.model_version is None:
        result_counter.model_version = result_future.result().model_spec.version.value
    result_counter.inc_done()
    result_counter.dec_active()
  return _callback


def pred_func(input, hostport, concurrency, num_tests):
  """Tests PredictionService with concurrent requests.
  Args:
    hostport: Host:port address of the PredictionService.
    concurrency: Maximum number of concurrent requests.
    num_tests: Number of test images to use.
  Returns:
    The classification error rate.
  Raises:
    IOError: An error occurred processing test data set.
  """
  channel = grpc.insecure_channel(hostport)
  stub = prediction_service_pb2_grpc.PredictionServiceStub(channel)
  result_counter = _ResultCounter(num_tests, concurrency)
  for i in range(num_tests):
    data = [input[i]]
    request = predict_pb2.PredictRequest()
    request.model_spec.name = 'multi_cnn_category_tf1_serving'
    # request.model_spec.signature_name = 'serving_default'
    request.inputs['input'].CopyFrom(
        tf.compat.v1.make_tensor_proto(data, dtype=float))
    result_counter.throttle()
    result_future = stub.Predict.future(request, 5.0)  # 5 seconds
    result_future.add_done_callback(_create_rpc_callback(result_counter))
  print('model_version used :', result_counter.get_model_version())
  return result_counter.get_predictions()

def main(_):
  if FLAGS.num_tests > 10000:
    print('num_tests should not be greater than 10k')
    return
  if not FLAGS.server:
    print('please specify server host:port')
    return
  # load the test data from dataset
  t1 = time()
  dataset = Dataset()
  input, _ = dataset.process_data(args.path_data_dir, args.vocab_file, args.path_stopwords, n_examples=FLAGS.num_tests)
  print("timed used for data loading :{:.4f}s".format(time() - t1))

  t2 = time()
  result = pred_func(input, FLAGS.server, FLAGS.concurrency, FLAGS.num_tests)
  print('num_tests: ', FLAGS.num_tests)
  print('concurrency: ', FLAGS.concurrency)
  if FLAGS.print_outputs:
    print(result)
  print("time_used: {:.4f}s".format(time() - t2))

if __name__ == '__main__':
  tf.compat.v1.app.run()

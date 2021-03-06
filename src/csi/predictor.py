# IRONA Server is subject to the terms of the Mozilla Public License 2.0.
# You can obtain a copy of MPL at LICENSE.md of repository root.

import pickle
import json
import threading as th
import socket as soc
import sys
import os
import multiprocessing as mp
import tensorflow as tf
import tensorflow.keras as keras
import tensorflow.keras.models as km
import tensorflow.keras.utils as ku
import tensorflow.keras.backend as kb
import numpy as np
import time
import logging
import gzip

# Set Print
def log(*args):
  print("\033[0;33m>> [PREDICTOR]\033[0m", " ".join(tuple(map(str, args))))

# Constants
[
  PROCESS_CMD,
  PRED_SOC,
  PIPE_TEMPLATE,
  MODEL_DIR,
  GPU_CONFIG,
  PREP_COUNT_STR,
  PIPE_BUFSIZE_STR,
  CSI_WINROW_STR,
  CSI_WINCOL_STR,
  PRED_INTERVAL_STR
] = sys.argv
PREP_COUNT = int(PREP_COUNT_STR)
PIPE_BUFSIZE = int(PIPE_BUFSIZE_STR)
CSI_WINROW = int(CSI_WINROW_STR)
CSI_WINCOL = int(CSI_WINCOL_STR)
PRED_INTERVAL = float(PRED_INTERVAL_STR)

# Set CUDA
if GPU_CONFIG != 'unset':
  os.environ['CUDA_VISIBLE_DEVICES'] = GPU_CONFIG

# Set Keras Core
MAX_CORE = mp.cpu_count()
os.environ['OMP_NUM_THREADS'] = str(MAX_CORE)
os.environ['KMP_AFFINITY'] = "noverbose,warnings,norespect,granularity=thread,scatter,0,0"
tf.config.threading.set_inter_op_parallelism_threads(MAX_CORE)
tf.config.threading.set_intra_op_parallelism_threads(MAX_CORE)

# Shut down tensorflow log
os.environ['TF_CPP_MIN_LOG_LEVEL'] = "2"
tf.get_logger().setLevel(logging.ERROR)

# Set File Names
modelName = MODEL_DIR + "/model.h5"
modelProp = MODEL_DIR + "/model.yml"
if not os.path.isfile(modelProp):
  modelProp = MODEL_DIR + "/model.json"
if not os.path.isfile(modelName):
  modelName = MODEL_DIR + "/model.hdf5"

# Use MultiWorkerMirroredStrategy
strategy = tf.distribute.MirroredStrategy()
log("Using gpu(s):", GPU_CONFIG, "({})".format(strategy.num_replicas_in_sync))
with strategy.scope():
  # Compile Model
  modelPropRaw = open(modelProp, 'r').read()
  if 'json' in modelProp:
    model = km.model_from_json(modelPropRaw)
  else:
    model = km.model_from_yaml(modelPropRaw)
  model.load_weights(modelName)
  model.compile(loss='categorical_crossentropy', optimizer='adam')
  model.predict(np.zeros((16, CSI_WINROW, CSI_WINCOL)))

  # Open IPC to Node
  waitListLock = th.Lock()
  waitList = np.empty([0, CSI_WINROW, CSI_WINCOL], float)
  waitAIDList = []
  PIPE_FORMFEED = "ｅｔｅｒｎｉｔｙ＿ＴａｋｅＭｙＨａｎｄ".encode("utf-8")
  with soc.socket(soc.AF_UNIX, soc.SOCK_STREAM) as node:
    node.connect(PRED_SOC)
    def predict():
      global log, model, waitListLock, waitList, node, waitAIDList
      global CSI_WINROW, CSI_WINCOL
      if len(waitList) > 0:
        log("Predicting now...")
        waitListLock.acquire()
        predList = waitList.copy()
        predAIDList = waitAIDList.copy()
        waitList = np.empty([0, CSI_WINROW, CSI_WINCOL], float)
        waitAIDList = []
        waitListLock.release()
        scores = model.predict(predList)
        node.send((json.dumps([scores.tolist(), predAIDList]) + '\f').encode())
      else:
        log("No pending windows.")

    class Predictor(th.Thread):
      def __init__ (self, sleep=8):
        th.Thread.__init__(self, name='Predictor')
        self.stop_event = th.Event()
        self.sleep = sleep
      def run (self):
        while self.sleep > 0 and not self.stop_event.is_set():
          th.Thread(target=predict).start()
          time.sleep(self.sleep)
      def stop (self):
        self.stop_event.set()
      def __enter__ (self):
        return self
      def __exit__ (self, *args, **kwargs):
        self.stop()

    def acceptWindow(i):
      global waitListLock, waitList, log, waitAIDList
      global PIPE_BUFSIZE, PIPE_TEMPLATE, PIPE_FORMFEED
      PIPE_SOC = PIPE_TEMPLATE.format(i)
      with soc.socket(soc.AF_UNIX, soc.SOCK_STREAM) as prep:
        prep.connect(PIPE_SOC)
        log("Connected to the preprocessor", i)
        while True:
          waitListBytes = b""
          while True:
            waitListBytes += prep.recv(65536)
            if waitListBytes[-57:] == PIPE_FORMFEED:
              waitListBytes = waitListBytes[:-57]
              break
          log("PIPE Data received.", "Length:", len(waitListBytes), "bytes")
          try:
            (aid, newList) = pickle.loads(gzip.decompress(waitListBytes))
            log("NP array loaded from PIPE data.")
            waitListLock.acquire()
            waitList = np.concatenate((waitList, newList), axis=0)
            waitAIDList = waitAIDList + ([aid] * len(newList))
            waitListLock.release()
          except pickle.UnpicklingError:
            log("Unpickling error occured. Packets discarded!")

    with Predictor(sleep=PRED_INTERVAL) as tp:
      for i in range(1, PREP_COUNT + 1):
        th.Thread(target=acceptWindow, args=(i,)).start()
      tp.start()
      tp.join()

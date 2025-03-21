# Copyright 2019 Uber Technologies, Inc. All Rights Reserved.
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
# ==============================================================================

import tensorflow as tf
import argparse
import numpy as np
import time 
import pandas as pd
# HVD-1 - initialize Horovd
import horovod.tensorflow as hvd
hvd.init()
print("# I am rank %d of %d" %(hvd.rank(), hvd.size()))


# HVD-2 - Assign GPUs to each rank
#gpus = tf.config.experimental.list_physical_devices('GPU')
#tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')


if (hvd.rank()==0):
            print('start profiler')

t0 = time.time()
parser = argparse.ArgumentParser(description='TensorFlow MNIST Example')
parser.add_argument('--batch_size', type=int, default=256, metavar='N',
                    help='input batch size for training (default: 256)')
parser.add_argument('--epochs', type=int, default=16, metavar='N',
                    help='number of epochs to train (default: 16)')
parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                    help='learning rate (default: 0.01)')
parser.add_argument('--device', default='gpu',
                    help='Wheter this is running on cpu or gpu')
parser.add_argument('--num_inter', default=2, help='set number inter', type=int)
parser.add_argument('--num_intra', default=0, help='set number intra', type=int)

args = parser.parse_args()


if args.device == 'cpu':
    tf.config.threading.set_intra_op_parallelism_threads(args.num_intra)
    tf.config.threading.set_inter_op_parallelism_threads(args.num_inter)
else:
    # HVD-2 - Assign GPUs to each rank
    gpus = tf.config.experimental.list_physical_devices('GPU')
    #tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')
    for gpu in gpus:
        tf.config.experimental.set_memory_growth(gpu, True)
    tf.config.experimental.set_visible_devices(gpus[hvd.local_rank()], 'GPU')


#---------------------------------------------------
# Dataset
#---------------------------------------------------
(mnist_images, mnist_labels), (x_test, y_test) = \
    tf.keras.datasets.mnist.load_data(path='mnist.npz')

dataset = tf.data.Dataset.from_tensor_slices(
    (tf.cast(mnist_images[..., tf.newaxis] / 255.0, tf.float32),
             tf.cast(mnist_labels, tf.int64))
)
test_dset = tf.data.Dataset.from_tensor_slices(
    (tf.cast(x_test[..., tf.newaxis] / 255.0, tf.float32),
             tf.cast(y_test, tf.int64))
)

nsamples = len(list(dataset))
ntests = len(list(test_dset))

# shuffle the dataset, with shuffle buffer to be 10000
dataset.shard(num_shards=hvd.size(), index=hvd.rank())
test_dset.shard(num_shards=hvd.size(), index=hvd.rank())
dataset = dataset.repeat().shuffle(10000).batch(args.batch_size)
test_dset  = test_dset.repeat().batch(args.batch_size)

#----------------------------------------------------
# Model
#----------------------------------------------------
mnist_model = tf.keras.Sequential([
    tf.keras.layers.Conv2D(32, [3, 3], activation='relu'),
    tf.keras.layers.Conv2D(64, [3, 3], activation='relu'),
    tf.keras.layers.MaxPooling2D(pool_size=(2, 2)),
    tf.keras.layers.Dropout(0.25),
    tf.keras.layers.Flatten(),
    tf.keras.layers.Dense(128, activation='relu'),
    tf.keras.layers.Dropout(0.5),
    tf.keras.layers.Dense(10, activation='softmax')
])
loss = tf.losses.SparseCategoricalCrossentropy()
# HVD-3 scale the learning rate
opt = tf.optimizers.Adam(args.lr*hvd.size())

checkpoint_dir = './checkpoints/tf2_mnist'
checkpoint = tf.train.Checkpoint(model=mnist_model, optimizer=opt)

#------------------------------------------------------------------
# Training
#------------------------------------------------------------------
@tf.function
def training_step(images, labels):
    with tf.GradientTape() as tape:
        probs = mnist_model(images, training=True)
        loss_value = loss(labels, probs)
        
        pred = tf.math.argmax(probs, axis=1)
        equality = tf.math.equal(pred, labels)
        accuracy = tf.math.reduce_mean(tf.cast(equality, tf.float32))
        
        
    # HVD-4 wrap the gradient tape
    tape = hvd.DistributedGradientTape(tape)
    grads = tape.gradient(loss_value, mnist_model.trainable_variables)
    opt.apply_gradients(zip(grads, mnist_model.trainable_variables))
    return loss_value, accuracy

@tf.function
def validation_step(images, labels):
    probs = mnist_model(images, training=False)
    pred = tf.math.argmax(probs, axis=1)
    equality = tf.math.equal(pred, labels)
    accuracy = tf.math.reduce_mean(tf.cast(equality, tf.float32))
    loss_value = loss(labels, probs)
    return loss_value, accuracy

from tqdm import tqdm 
t0 = time.time()

nstep = nsamples//args.batch_size//hvd.size()
ntest_step = ntests//args.batch_size//hvd.size()
metrics={}
metrics['train_acc'] = []
metrics['valid_acc'] = []
metrics['train_loss'] = []
metrics['valid_loss'] = []
metrics['time_per_epochs'] = []
for ep in range(args.epochs):
    training_loss = 0.0
    training_acc = 0.0
    tt0 = time.time()
    for batch, (images, labels) in enumerate(dataset.take(nstep)):
        loss_value, acc = training_step(images, labels)

        ##Averaging 
        loss_value = hvd.allreduce(loss_value, average=True)
        acc = hvd.allreduce(acc, average=True)

   #     training_loss += loss_value/nstep
    #    training_acc += acc/nstep
        
        #training_loss = hvd.allreduce(training_loss, average=True)
        #training_acc = hvd.allreduce(training_acc, average=True)

        ##HVD-5 broadacast variables from rank 0
        if (nstep==0 and ep== 0 and hvd.rank()==0):
            hvd.broadcast_variables(mnist_model.variables(), root_rank=0)
            hvd.broadcast_variables(opt.variables(), root_rank=0)
        
        training_loss += loss_value/nstep
        training_acc += acc/nstep

        
        if (batch % 100 == 0 and hvd.rank()==0):
            checkpoint.save(checkpoint_dir)
            print('Epoch - %d, step #%06d/%06d\tLoss: %.6f' % (ep, batch, nstep, loss_value))
    training_loss = hvd.allreduce(training_loss, average=True)
    training_acc = hvd.allreduce(training_acc, average=True)
    # Testing
    test_acc = 0.0
    test_loss = 0.0
    for batch, (images, labels) in enumerate(test_dset.take(ntest_step)):
        loss_value, acc = validation_step(images, labels)
        #
        #loss_value = hvd.allreduce(loss_value, average=True)
        #acc = hvd.allreduce(acc, average=True)

        test_acc += acc / ntest_step
        test_loss += loss_value / ntest_step

        #test_loss = hvd.allreduce(test_loss, average=True)
        #test_acc = hvd.allreduce(test_acc, average=True)

    tt1 = time.time()
    #test_loss /=ntest_step
    #test_acc /= ntest_step
    # HVD-8 average the metrics
    test_loss = hvd.allreduce(test_loss, average=True)
    test_acc = hvd.allreduce(test_acc, average=True)
   
    
    if (hvd.rank()==0):
        print('E[%d], train Loss: %.6f, training Acc: %.3f, val loss: %.3f, val Acc: %.3f\t Time: %.3f seconds' % (ep, training_loss, training_acc, test_loss, test_acc, tt1 - tt0))
        metrics['train_acc'].append(training_acc.numpy())
        metrics['train_loss'].append(training_loss.numpy())
        metrics['valid_acc'].append(test_acc.numpy())
        metrics['valid_loss'].append(test_loss.numpy())
        metrics['time_per_epochs'].append(tt1 - tt0) 
if (hvd.rank()==0):
    checkpoint.save(checkpoint_dir)
    data_shuffle_2=pd.DataFrame({'epoch':np.arange(16),'train_acc': np.array(metrics['train_acc'])
                               , 'train_loss': np.array(metrics['train_loss'])
                               ,'valid_acc':np.array(metrics['valid_acc'])
                              ,'valid_loss':np.array(metrics['valid_loss'])})
    data_shuffle_2.to_csv("data_"+str(hvd.size())+".csv")
    np.savetxt("metrics.dat", np.array([metrics['train_acc'], metrics['train_loss'], metrics['valid_acc'], metrics['valid_loss'], metrics['time_per_epochs']]).transpose())
t1 = time.time()
if (hvd.rank()==0):
    print("Total training time: %s seconds" %(t1 - t0))

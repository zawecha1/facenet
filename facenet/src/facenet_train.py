"""Training a face recognizer with TensorFlow based on the FaceNet paper

FaceNet: A Unified Embedding for Face Recognition and Clustering: http://arxiv.org/abs/1503.03832
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from datetime import datetime
import os.path
import time
import sys
import tensorflow as tf
import numpy as np
import importlib
import argparse
import facenet
import lfw

def main(args):
  
    network = importlib.import_module(args.model_def, 'inference')

    if args.model_name:
        subdir = args.model_name
        preload_model = True
    else:
        subdir = datetime.strftime(datetime.now(), '%Y%m%d-%H%M%S')
        preload_model = False
    log_dir = os.path.join(os.path.expanduser(args.logs_base_dir), subdir)
    if not os.path.isdir(log_dir):  # Create the log directory if it doesn't exist
        os.makedirs(log_dir)
    model_dir = os.path.join(os.path.expanduser(args.models_base_dir), subdir)
    if not os.path.isdir(model_dir):  # Create the model directory if it doesn't exist
        os.makedirs(model_dir)

    # Store some git revision info in a text file in the log directory
    src_path,_ = os.path.split(os.path.realpath(__file__))
    facenet.store_revision_info(src_path, log_dir, ' '.join(sys.argv))

    np.random.seed(seed=args.seed)
    train_set = facenet.get_dataset(args.data_dir)
    print('Model directory: %s' % model_dir)
    print('Log directory: %s' % log_dir)
    
    with tf.Graph().as_default():
        tf.set_random_seed(args.seed)
        global_step = tf.Variable(0, trainable=False)

        # Placeholder for input images
        images_placeholder = tf.placeholder(tf.float32, shape=(None, args.image_size, args.image_size, 3), name='input')

        # Placeholder for phase_train
        phase_train_placeholder = tf.placeholder(tf.bool, name='phase_train')

        # Placeholder for the learning rate
        learning_rate_placeholder = tf.placeholder(tf.float32, name='learing_rate')
        
        # Build the inference graph
        logits1, logits2 = network.inference(images_placeholder, [ 128, len(train_set) ], args.keep_probability, 
        phase_train=phase_train_placeholder, weight_decay=args.weight_decay)
        embeddings = tf.nn.l2_normalize(logits1, 1, 1e-10, name='embeddings')
        # Split example embeddings into anchor, positive and negative
        anchor, positive, negative = tf.split(0, 3, embeddings)

        # Calculate triplet loss
        loss = facenet.triplet_loss(anchor, positive, negative, args.alpha)
       
        learning_rate = tf.train.exponential_decay(learning_rate_placeholder, global_step,
        args.learning_rate_decay_epochs*args.epoch_size, args.learning_rate_decay_factor, staircase=True)
        tf.scalar_summary('learning_rate', learning_rate)

        # Calculate the total loss
        regularization_losses = tf.get_collection(tf.GraphKeys.REGULARIZATION_LOSSES)
        total_loss = tf.add_n([loss] + regularization_losses, name='total_loss')

        # Build a Graph that trains the model with one batch of examples and updates the model parameters
        train_op = facenet.train("",total_loss, global_step, args.optimizer, learning_rate, args.moving_average_decay)

        # Create a saver
        saver = tf.train.Saver(tf.all_variables(), max_to_keep=0)

        # Build the summary operation based on the TF collection of Summaries.
        summary_op = tf.merge_all_summaries()

        # Build an initialization operation to run below.
        init = tf.initialize_all_variables()

        # Start running operations on the Graph.
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=args.gpu_memory_fraction)
        sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options))

        sess.run(init)
        summary_writer = tf.train.SummaryWriter(log_dir, sess.graph)

        with sess.as_default():

            if preload_model:
                ckpt = tf.train.get_checkpoint_state(model_dir)
                #pylint: disable=maybe-no-member
                if ckpt and ckpt.model_checkpoint_path:
                    saver.restore(sess, ckpt.model_checkpoint_path)
                else:
                    raise ValueError('Checkpoint not found')
            # Read the file containing the pairs used for testing
            pairs = lfw.read_pairs(os.path.expanduser(args.lfw_pairs))

            # Get the paths for the corresponding images
            paths, actual_issame = lfw.get_paths(os.path.expanduser(args.lfw_dir), pairs, args.lfw_file_ext)

            # Training and validation loop
            epoch = sess.run(global_step, feed_dict=None) // args.epoch_size
            while epoch < args.max_nrof_epochs:
            #for epoch in range(args.max_nrof_epochs):
                # Train for one epoch
                step = train(args, sess, train_set, epoch, images_placeholder, phase_train_placeholder,
                        learning_rate_placeholder, global_step, embeddings, loss, train_op, summary_op, summary_writer)
                _, _, accuracy, val, val_std, far = lfw.validate(sess,
                    paths, actual_issame, args.seed, args.batch_size,
                    images_placeholder, phase_train_placeholder, embeddings, nrof_folds=args.lfw_nrof_folds)
                print('Accuracy: %1.3f+-%1.3f' % (np.mean(accuracy), np.std(accuracy)))
                print('Validation rate: %2.5f+-%2.5f @ FAR=%2.5f' % (val, val_std, far))
                # Add validation loss and accuracy to summary
                summary = tf.Summary()
                #pylint: disable=maybe-no-member
                summary.value.add(tag='lfw/accuracy', simple_value=np.mean(accuracy))
                summary.value.add(tag='lfw/val_rate', simple_value=val)
                summary_writer.add_summary(summary, step)

                if (epoch % args.checkpoint_period == 0) or (epoch==args.max_nrof_epochs-1):
                    # Save the model checkpoint
                    print('Saving checkpoint')
                    checkpoint_path = os.path.join(model_dir, 'model.ckpt')
                    saver.save(sess, checkpoint_path, global_step=step)
    return model_dir


def train(args, sess, dataset, epoch, images_placeholder, phase_train_placeholder,
          learning_rate_placeholder, global_step, embeddings, loss, train_op, summary_op, summary_writer): #each epoch
    batch_number = 0
    if args.learning_rate>0.0:
        lr = args.learning_rate
    else:
        lr = get_learning_rate_from_file('../data/learning_rate_schedule.txt', epoch)

    while batch_number < args.epoch_size: #this batch_number means batch_num in train, not in getting feature
        print('Loading training data')
        # Sample people and load new data
        start_time = time.time()
        image_paths, num_per_class = facenet.sample_people(dataset, args.people_per_epoch, args.images_per_person)
        image_data = facenet.load_data(image_paths, args.random_crop, args.random_flip, args.image_size)
        load_time = time.time() - start_time
        print('Loaded %d images in %.2f seconds' % (image_data.shape[0], load_time))

        print('Selecting suitable triplets for training')
        start_time = time.time()
        emb_list = []
        # Run a forward pass for the sampled images
        nrof_examples_per_epoch = args.people_per_epoch * args.images_per_person#have no connection with epoch_size
        nrof_batches_per_epoch = int(np.floor(nrof_examples_per_epoch / args.batch_size))#the variable is only for batched in getting feature
        for i in xrange(nrof_batches_per_epoch):
            batch = facenet.get_batch(image_data, args.batch_size, i)
            feed_dict = {images_placeholder: batch, phase_train_placeholder: True, learning_rate_placeholder: lr}
            emb_list += sess.run([embeddings], feed_dict=feed_dict)#step 1:get the feature of all image in a epoch
        emb_array = np.vstack(emb_list)  # Stack the embeddings to a nrof_examples_per_epoch x 128 matrix
        # Select triplets based on the embeddings
        # the data from lfw dataset makes the embeddings'size different from each other
        triplets, nrof_random_negs, nrof_triplets = facenet.select_triplets(
            emb_array, num_per_class, image_data, args.people_per_epoch, args.alpha)#Here the information from embedding and information from sample_people()
        #print("num_triplets: "+str(len(triplets)))
        selection_time = time.time() - start_time
        print('(nrof_random_negs, nrof_triplets) = (%d, %d): time=%.3f seconds' % (
        nrof_random_negs, nrof_triplets, selection_time))

        # Perform training on the selected triplets
        train_time = 0
        i = 0
        #if tripplets is less than batch_size, go back to the start of the epoch
        while i * args.batch_size < nrof_triplets * 3 and batch_number < args.epoch_size:
        #Why not refresh the triplet every batch
            start_time = time.time()
            batch = facenet.get_triplet_batch(triplets, i, args.batch_size)
            feed_dict = {images_placeholder: batch, phase_train_placeholder: True, learning_rate_placeholder: lr}
            err, _, step = sess.run([loss, train_op, global_step], feed_dict=feed_dict)
            if (batch_number % 20 == 1):
                #does the line below waste time?
                summary_str, step = sess.run([summary_op, global_step], feed_dict=feed_dict)
                summary_writer.add_summary(summary_str, global_step=step)
            duration = time.time() - start_time
            print('Epoch: [%d][%d/%d]\tTime %.3f\ttripErr %2.3f' %
                  (epoch, batch_number, args.epoch_size, duration, err))
            batch_number += 1
            i += 1
            train_time += duration
        # Add validation loss and accuracy to summary
        summary = tf.Summary()
        #pylint: disable=maybe-no-member
        summary.value.add(tag='time/load', simple_value=load_time)
        summary.value.add(tag='time/selection', simple_value=selection_time)
        summary.value.add(tag='time/train', simple_value=train_time)
        summary.value.add(tag='time/total', simple_value=load_time+selection_time+train_time)
        summary_writer.add_summary(summary, step)
    return step
def get_learning_rate_from_file(filename, epoch):
    with open(filename, 'r') as f:
          for line in f.readlines():
              line = line.split('#', 1)[0]
              if line:
                  par = line.strip().split(':')
                  e = int(par[0])
                  lr = float(par[1])
                  if e <= epoch:
                      learning_rate = lr
                  else:
                      return learning_rate

def parse_arguments(argv):
    parser = argparse.ArgumentParser()
    
    parser.add_argument('--logs_base_dir', type=str, 
        help='Directory where to write event logs.', default='../logs/facenet')
    parser.add_argument('--models_base_dir', type=str,
        help='Directory where to write trained models and checkpoints.', default='./models/facenet')
    parser.add_argument('--model_name', type=str,
        help='Model directory name. Used when continuing training of an existing model. Leave empty to train new model.')
    parser.add_argument('--data_dir', type=str,
        help='Path to the data directory containing aligned face patches. Multiple directories are separated with colon.',
        default='../data/lfw_aligned_96/')
    parser.add_argument('--model_def', type=str,
        help='Model definition. Points to a module containing the definition of the inference graph.', default='models.nn4')
    parser.add_argument('--max_nrof_epochs', type=int,
        help='Number of epochs to run.', default=500)
    parser.add_argument('--checkpoint_period', type=int,
        help='The number of epochs between checkpoints', default=5)
    parser.add_argument('--batch_size', type=int,
        help='Number of images to process in a batch.Need to divide people_per_epoch*images_per_person exactly.', default=100)
    parser.add_argument('--image_size', type=int,
        help='Image size (height, width) in pixels.', default=96)
    parser.add_argument('--people_per_epoch', type=int,
        help='Number of people per epoch.', default=25)
    parser.add_argument('--images_per_person', type=int,
        help='Number of images per person.', default=20)
    parser.add_argument('--epoch_size', type=int,
        help='Number of batches per epoch.', default=1000)
    parser.add_argument('--alpha', type=float,
        help='Positive to negative triplet distance margin.', default=0.2)
    parser.add_argument('--random_crop', 
        help='Performs random cropping of training images. If false, the center image_size pixels from the training images are used. ' +
         'If the size of the images in the data directory is equal to image_size no cropping is performed', action='store_true')
    parser.add_argument('--random_flip', 
        help='Performs random horizontal flipping of training images.', action='store_true')
    parser.add_argument('--pool_type', type=str,
        help='The type of pooling to use for some of the inception layers', default='MAX', choices=['MAX', 'L2'])
    parser.add_argument('--use_lrn', 
        help='Enables Local Response Normalization after the first layers of the inception network.', action='store_true')
    parser.add_argument('--keep_probability', type=float,
        help='Keep probability of dropout for the fully connected layer(s).', default=0.8)
    parser.add_argument('--weight_decay', type=float,
        help='L2 weight regularization.', default=0.1)
    parser.add_argument('--optimizer', type=str, choices=['ADAGRAD', 'ADADELTA', 'ADAM'],
        help='The optimization algorithm to use', default='ADAGRAD')
    parser.add_argument('--learning_rate', type=float,
        help='Initial learning rate.', default=0.1)
    parser.add_argument('--learning_rate_decay_epochs', type=int,
        help='Number of epochs between learning rate decay.', default=10)
    parser.add_argument('--learning_rate_decay_factor', type=float,
        help='Learning rate decay factor.', default=0.95)
    parser.add_argument('--moving_average_decay', type=float,
        help='Exponential decay for tracking of training parameters.', default=0.999)
    parser.add_argument('--seed', type=int,
        help='Random seed.', default=666)
 
    parser.add_argument('--gpu_memory_fraction', type=float,
    help='Upper bound on the amount of GPU memory that will be used by the process.', default=0.8)
    # Parameters for validation on LFW
    parser.add_argument('--lfw_pairs', type=str,
        help='The file containing the pairs to use for validation.', default='../data/pairs.txt')
    parser.add_argument('--lfw_file_ext', type=str,
        help='The file extension for the LFW dataset.', default='png', choices=['jpg', 'png'])
    parser.add_argument('--lfw_dir', type=str,
        help='Path to the data directory containing aligned face patches.', default='../data/lfw_aligned_96/')
    parser.add_argument('--lfw_nrof_folds', type=int,
        help='Number of folds to use for cross validation. Mainly used for testing.', default=10)
    return parser.parse_args(argv)
  

if __name__ == '__main__':
    main(parse_arguments(sys.argv[1:]))
import abc
from enum import Enum
import os
import tensorflow as tf
from .flowlib import flow_to_image, write_flow
import numpy as np
from scipy.misc import imread, imsave
import uuid
from .training_schedules import LONG_SCHEDULE
from .utils import pad
from .extras import sincos_norm,sincos2r,warper, merge_rt,pose2mat
from hyperparams import batch_size,archi,fine_tune, quater, do_avgpooling,pretrained_flow, Mode
from dump2disk import *
slim = tf.contrib.slim


class Mode(Enum):
    TRAIN = 1
    TEST = 2


class Net(object):
    __metaclass__ = abc.ABCMeta

    def __init__(self, mode=Mode.TRAIN, debug=False):
        self.global_step = slim.get_or_create_global_step()
        self.mode = mode
        self.debug = debug

    @abc.abstractmethod
    def model(self, inputs, training_schedule, trainable=True):
        """
        Defines the model and returns a tuple of Tensors needed for calculating the loss.
        """
        return

    @abc.abstractmethod
    def loss(self, **kwargs):
        """
        Accepts prediction Tensors from the output of `model`.
        Returns a single Tensor representing the total loss of the model.
        """
        return
    def euro(self, inputs):
        bs=batch_size
        training_schedule = LONG_SCHEDULE
        if Mode=='test'or ((not fine_tune) and (pretrained_flow)):
            predictions = self.model(inputs, training_schedule, trainable=False)
        else:
            predictions = self.model(inputs, training_schedule, trainable=True)

        if(archi=="Flownetc"):
            fuse_interconv0 = predictions['predict_flow2']
        elif(archi=="Flownet2"):
            fuse_interconv0 = predictions['fuse_interconv0']
        else:
            raise('didnot choose architecture')
            
        with slim.arg_scope([slim.fully_connected,slim.conv2d],
                        activation_fn=None,
                        # normalizer_fn=slim.batch_norm,
                        weights_initializer=\
                        tf.truncated_normal_initializer(stddev=0.01),
                        weights_regularizer=slim.l2_regularizer(0.0005),trainable=True):
                        if not quater:
                            if not do_avgpooling:
                                conv_l=slim.conv2d(fuse_interconv0,1,3,activation_fn=None, scope='f1')
                                conv_l=tf.contrib.layers.flatten(conv_l)
                                pred = slim.fully_connected(conv_l, 9,activation_fn=None,scope="f2")
                            else:
                                conv_l=slim.conv2d(fuse_interconv0,9,3,activation_fn=None, scope='f1')
                                pred = slim.avg_pool2d(conv_l,[80,112],stride=2,padding='VALID',scope="f2")
                                pred=tf.reshape(pred,[bs,9])

                            sin = tf.slice(pred, [0,0], [-1,3])*0.001
                            cos = tf.slice(pred, [0,3], [-1,3])*0.001
                            tra = tf.slice(pred, [0,6], [-1,3])
                            [sina,  sinb, sing] = tf.unstack(sin,axis=1)
                            [cosa, cosb, cosg] = tf.unstack(cos,axis=1)

                            # normalize
                            sina, cosa = sincos_norm(sina, cosa)
                            sinb, cosb = sincos_norm(sinb, cosb)
                            sing, cosg = sincos_norm(sing, cosg)

                            R = sincos2r(sina,sinb,sing,cosa,cosb,cosg)
                            T = tra

                            RT = merge_rt(R,T)
                        else:
                            if not do_avgpooling:
                                conv_l=slim.conv2d(fuse_interconv0,1,3,activation_fn=None, scope='f1')
                                conv_l=tf.contrib.layers.flatten(conv_l)
                                pred = slim.fully_connected(conv_l, 7,activation_fn=None,scope="f2")
                                # pred_t = slim.fully_connected(conv_l, 3,activation_fn=None,scope="f3")
                                # pred=tf.concat([pred_t,pred_q],axis=1)
                            else:
                                conv_l=slim.conv2d(fuse_interconv0,7,3,activation_fn=None, scope='f1')
                                pred = slim.avg_pool2d(conv_l,[80,112],stride=2,padding='VALID',scope="f2")
                                pred=tf.reshape(pred,[bs,7])
                                
                            RT= pose2mat(pred)
    
                        return RT                 

    def flowtest(self,inputs):
         # TODO: This is a hack, we should get rid of this
        training_schedule = LONG_SCHEDULE
        predictions = self.model(inputs, training_schedule)
        pred_flow = predictions['flow']

        # Scale output flow relative to input size
        # pred_flow = pred_flow * [inputs['input_a'].shape.as_list()[2],
                                 # inputs['input_a'].shape.as_list()[1]]
        return pred_flow

    def test(self, checkpoint, input_a_path, input_b_path, out_path, save_image=True, save_flo=False):
        input_a = imread(input_a_path)
        input_b = imread(input_b_path)

        # # Convert from RGB -> BGR
        input_a = input_a[..., [2, 1, 0]]
        input_b = input_b[..., [2, 1, 0]]

        # # Scale from [0, 255] -> [0.0, 1.0] if needed
        if input_a.max() > 1.0:
            input_a = input_a / 255.0
        if input_b.max() > 1.0:
            input_b = input_b / 255.0

        # TODO: This is a hack, we should get rid of this
        training_schedule = LONG_SCHEDULE

        inputs = {
            'input_a': tf.expand_dims(tf.constant(input_a, dtype=tf.float32), 0),
            'input_b': tf.expand_dims(tf.constant(input_b, dtype=tf.float32), 0),
        }
        predictions = self.model(inputs, training_schedule)
        pred_flow = predictions['flow']
        saver = tf.train.Saver()
        (i2_warped,occ)=warper(inputs['input_b'], pred_flow)
        with tf.Session() as sess:
            saver.restore(sess, checkpoint)
            [pred_flow_,i2_warped_] = sess.run([pred_flow,i2_warped])
            pred_flow_=pred_flow_[0, :, :, :]
        x_flow=(pred_flow_[:,:,0])
        #baseline
        b=-36.5
        f=1
        # f=332.38475539
        Z=np.divide(b*f,x_flow)
        # Z[abs(Z)>50]=0
        Z=np.expand_dims(Z,axis=0)
        Z=np.expand_dims(Z,axis=3)
        Z=np.tile(Z,[1,1,1,3])
        # m=np.mean(Z)
        #blue if closer
        Z[0,abs(Z[0,:,:,2])>5,2]=0
        #green if middle
        Z[0,np.logical_or(abs(Z[0,:,:,1])<3, abs(Z[0,:,:,1])>10),1]=0
        #red far away
        Z[0,np.logical_or(abs(Z[0,:,:,0])<7, abs(Z[0,:,:,0])>20),0]=0
        print(Z)

        print(np.shape(x_flow))
        input_a=np.expand_dims(input_a, axis=0)
        input_b=np.expand_dims(input_b, axis=0)
        i=4
        dump2disk(out_path, i, input_a,input_b,i2_warped_,Z)
        flow_img = flow_to_image(pred_flow_)
        full_out_path = os.path.join(out_path, "flow_"+'{:08}'.format(i)  + '.png')
        imsave(full_out_path, flow_img)
            # unique_name = 'flow-' + str(uuid.uuid4())
            # if save_image:
            #     flow_img = flow_to_image(pred_flow)
            #     full_out_path = os.path.join(out_path, unique_name + '.png')
            #     imsave(full_out_path, flow_img)

            # if save_flo:
            #     full_out_path = os.path.join(out_path, unique_name + '.flo')
            #     write_flow(pred_flow, full_out_path)

    def train(self, log_dir, training_schedule, input_a, input_b, flow, checkpoints=None):
        tf.summary.image("image_a", input_a, max_outputs=2)
        tf.summary.image("image_b", input_b, max_outputs=2)

        self.learning_rate = tf.train.piecewise_constant(
            self.global_step,
            [tf.cast(v, tf.int64) for v in training_schedule['step_values']],
            training_schedule['learning_rates'])

        optimizer = tf.train.AdamOptimizer(
            self.learning_rate,
            training_schedule['momentum'],
            training_schedule['momentum2'])

        inputs = {
            'input_a': input_a,
            'input_b': input_b,
        }
        predictions = self.model(inputs, training_schedule)
        total_loss = self.loss(flow, predictions)
        tf.summary.scalar('loss', total_loss)

        if checkpoints:
            for (checkpoint_path, (scope, new_scope)) in checkpoints.iteritems():
                variables_to_restore = slim.get_variables(scope=scope)
                renamed_variables = {
                    var.op.name.split(new_scope + '/')[1]: var
                    for var in variables_to_restore
                }
                restorer = tf.train.Saver(renamed_variables)
                with tf.Session() as sess:
                    restorer.restore(sess, checkpoint_path)

        # Show the generated flow in TensorBoard
        if 'flow' in predictions:
            pred_flow_0 = predictions['flow'][0, :, :, :]
            pred_flow_0 = tf.py_func(flow_to_image, [pred_flow_0], tf.uint8)
            pred_flow_1 = predictions['flow'][1, :, :, :]
            pred_flow_1 = tf.py_func(flow_to_image, [pred_flow_1], tf.uint8)
            pred_flow_img = tf.stack([pred_flow_0, pred_flow_1], 0)
            tf.summary.image('pred_flow', pred_flow_img, max_outputs=2)

        true_flow_0 = flow[0, :, :, :]
        true_flow_0 = tf.py_func(flow_to_image, [true_flow_0], tf.uint8)
        true_flow_1 = flow[1, :, :, :]
        true_flow_1 = tf.py_func(flow_to_image, [true_flow_1], tf.uint8)
        true_flow_img = tf.stack([true_flow_0, true_flow_1], 0)
        tf.summary.image('true_flow', true_flow_img, max_outputs=2)

        train_op = slim.learning.create_train_op(
            total_loss,
            optimizer,
            summarize_gradients=True)

        if self.debug:
            with tf.Session() as sess:
                sess.run(tf.global_variables_initializer())
                tf.train.start_queue_runners(sess)
                slim.learning.train_step(
                    sess,
                    train_op,
                    self.global_step,
                    {
                        'should_trace': tf.constant(1),
                        'should_log': tf.constant(1),
                        'logdir': log_dir + '/debug',
                    }
                )
        else:
            slim.learning.train(
                train_op,
                log_dir,
                # session_config=tf.ConfigProto(allow_soft_placement=True),
                global_step=self.global_step,
                save_summaries_secs=60,
                number_of_steps=training_schedule['max_iter']
            )

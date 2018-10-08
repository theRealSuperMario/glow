import tensorflow as tf

import src.glow.tfops as Z
import src.glow.optim as optim
import numpy as np
import horovod.tensorflow as hvd
from tensorflow.contrib.framework.python.ops import add_arg_scope
from sklearn.metrics import roc_curve, roc_auc_score


'''
f_loss: function with as input the (x,y,reuse=False), and as output a list/tuple whose first element is the loss.
'''


def abstract_model_xy(sess, hps, feeds, train_iterator, test_iterator, data_init, lr, f_loss, log_prob_iterator = None):

    # == Create class with static fields and methods
    class m(object):
        pass
    m.sess = sess
    m.feeds = feeds
    m.lr = lr

    # === Loss and optimizer
    loss_train, stats_train = f_loss(train_iterator, True)
    all_params = tf.trainable_variables()
    if hps.gradient_checkpointing == 1:
        from src.glow.memory_saving_gradients import gradients
        gs = gradients(loss_train, all_params)
    else:
        gs = tf.gradients(loss_train, all_params)

    optimizer = {'adam': optim.adam, 'adamax': optim.adamax,
                 'adam2': optim.adam2}[hps.optimizer]

    train_op, polyak_swap_op, ema = optimizer(
        all_params, gs, alpha=lr, hps=hps)
    if hps.direct_iterator:
        m.train = lambda _lr: sess.run([train_op, stats_train], {lr: _lr})[1]
    else:
        def _train(_lr):
            _x, _y = train_iterator()
            return sess.run([train_op, stats_train], {feeds['x']: _x,
                                                      feeds['y']: _y, lr: _lr})[1]
        m.train = _train

    m.polyak_swap = lambda: sess.run(polyak_swap_op)

    # === Testing
    loss_test, stats_test = f_loss(test_iterator, False, reuse=True)
    if hps.direct_iterator:
        m.test = lambda: sess.run(stats_test)
    else:
        def _test():
            _x, _y = test_iterator()
            return sess.run(stats_test, {feeds['x']: _x,
                                         feeds['y']: _y})
        m.test = _test

    # === logprobs on test
    res_probs = log_prob_iterator(test_iterator, False, reuse=True)
    if hps.direct_iterator:
        m.probs = lambda: sess.run(res_probs)
    else:
        def _probs():
            _x, _y = test_iterator()
            return sess.run(res_probs, {feeds['x']: _x,
                                         feeds['y']: _y})
        m.probs = _probs

    # === Saving and restoring
    saver = tf.train.Saver()
    saver_ema = tf.train.Saver(ema.variables_to_restore())
    m.save_ema = lambda path: saver_ema.save(
        sess, path, write_meta_graph=False)
    m.save = lambda path: saver.save(sess, path, write_meta_graph=False)
    m.restore = lambda path: saver.restore(sess, path)

    # === Initialize the parameters
    if hps.restore_path != '':
        m.restore(hps.restore_path)
    else:
        with Z.arg_scope([Z.get_variable_ddi, Z.actnorm], init=True):
            results_init = f_loss(None, True, reuse=True)
        sess.run(tf.global_variables_initializer())
        sess.run(results_init, {feeds['x']: data_init['x'],
                                feeds['y']: data_init['y']})
    sess.run(hvd.broadcast_global_variables(0))

    return m


def codec(hps):

    def encoder(z, objective):

        for i in range(hps.n_levels):
            z, objective = revnet2d(str(i), z, objective, hps)
            if i < hps.n_levels-1:
                z, objective = split2d("pool"+str(i), z, objective=objective)

        return z, objective

    def decoder(z, eps_std):

        for i in reversed(range(hps.n_levels)):
            if i < hps.n_levels-1:
                z = split2d_reverse("pool"+str(i), z, eps_std=eps_std)
            z, _ = revnet2d(str(i), z, 0, hps, reverse=True)

        return z

    return encoder, decoder


def prior(name, y_onehot, hps):

    with tf.variable_scope(name):
        n_z = hps.top_shape[-1]

        top_shape = hps.top_shape
        if type(top_shape) == tuple:
            top_shape = list(top_shape)
            print(top_shape)


        h = tf.zeros([tf.shape(y_onehot)[0]] + top_shape[:2]+[2*n_z])
        if hps.learntop:
            h = Z.conv2d_zeros('p', h, 2*n_z)
        if hps.ycond:
            h += tf.reshape(Z.linear_zeros("y_emb", y_onehot,
                                           2*n_z), [-1, 1, 1, 2 * n_z])

        pz = Z.gaussian_diag(h[:, :, :, :n_z], h[:, :, :, n_z:])
        # first half of last dimension is mean, second half is sigma

    def logp(z1):
        objective = pz.logp(z1)
        return objective

    def sample(eps_std=None):
        if eps_std is not None:
            z = pz.sample2(pz.eps * tf.reshape(eps_std, [-1, 1, 1, 1]))
        else:
            z = pz.sample

        return z

    return logp, sample


def model(sess, hps, train_iterator, test_iterator, data_init):
    '''

    Args:
        sess:
        hps:
        train_iterator:
        test_iterator:
        data_init:

    Returns:

    '''

    # Only for decoding/init, rest use iterators directly
    with tf.name_scope('input'):
        X = tf.placeholder(
            tf.uint8, [None, hps.image_size, hps.image_size, 3], name='image')
        Y = tf.placeholder(tf.int32, [None], name='label')
        lr = tf.placeholder(tf.float32, None, name='learning_rate')

    encoder, decoder = codec(hps)
    hps.n_bins = 2. ** hps.n_bits_x

    def preprocess(x):
        x = tf.cast(x, 'float32')
        if hps.n_bits_x < 8:
            x = tf.floor(x / 2 ** (8 - hps.n_bits_x))
        x = x / hps.n_bins - .5
        return x

    def postprocess(x):
        return tf.cast(tf.clip_by_value(tf.floor((x + .5)*hps.n_bins)*(256./hps.n_bins), 0, 255), 'uint8')

    def _f_loss(x, y, is_training, reuse=False):

        with tf.variable_scope('model', reuse=reuse):
            y_onehot = tf.cast(tf.one_hot(y, hps.n_y, 1, 0), 'float32')

            objective = tf.zeros_like(x, dtype='float32')[:, 0, 0, 0]

            z = preprocess(x)
            z = z + tf.random_uniform(tf.shape(z), 0, 1./hps.n_bins)

            objective += - np.log(hps.n_bins) * np.prod(Z.int_shape(z)[1:])

            # Encode
            z = Z.squeeze2d(z, 2)  # > 16x16x12

            #z, logdet
            z, objective = encoder(z, objective)

            hps.top_shape = Z.int_shape(z)[1:]

            # Prior
            logp, _ = prior("prior", y_onehot, hps)
            objective += logp(z)

            # Generative loss
            nobj = - objective
            bits_x = nobj / (np.log(2.) * int(x.get_shape()[1]) * int(
                x.get_shape()[2]) * int(x.get_shape()[3]))  # bits per subpixel
            #bits_x = log_2(p(x))*(x-dim)*(y-dim)*(# channel)

            # Predictive loss
            if hps.weight_y > 0 and hps.ycond:

                # Classification loss
                h_y = tf.reduce_mean(z, axis=[1, 2])

                y_logits = Z.linear_zeros("classifier", h_y, hps.n_y)
                bits_y = tf.nn.softmax_cross_entropy_with_logits_v2(
                    labels=y_onehot, logits=y_logits) / np.log(2.)

                # Classification accuracy
                y_predicted = tf.argmax(y_logits, 1, output_type=tf.int32)
                classification_error = 1 - \
                    tf.cast(tf.equal(y_predicted, y), tf.float32)
            else:
                bits_y = tf.zeros_like(bits_x)
                classification_error = tf.ones_like(bits_x)

        return bits_x, bits_y, classification_error

    def log_prob(x, y, reuse=False):
        y_onehot = tf.cast(tf.one_hot(y, hps.n_y, 1, 0), 'float32')
        with tf.variable_scope('model', reuse=reuse):
            objective = tf.zeros_like(x, dtype='float32')[:, 0, 0, 0]


    # === Sampling function
    def f_decode(y, eps_std):
        y_onehot = tf.cast(tf.one_hot(y, hps.n_y, 1, 0), 'float32')
        with tf.variable_scope('model', reuse=True):

            _, sample = prior("prior", y_onehot, hps)
            z = sample(eps_std)

            z = decoder(z, eps_std)

            z = Z.unsqueeze2d(z, 2)  # 8x8x12 -> 16x16x3

            x = postprocess(z)

        return x

    def f_loss(iterator, is_training, reuse=False):
        if hps.direct_iterator and iterator is not None:
            x, y = iterator.get_next()
        else:
            x, y = X, Y

        bits_x, bits_y, pred_loss = _f_loss(x, y, is_training, reuse)

        local_loss = bits_x + hps.weight_y * bits_y

        stats = [local_loss, bits_x, bits_y, pred_loss]
        global_stats = Z.allreduce_mean(
            tf.stack([tf.reduce_mean(i) for i in stats]))

        return tf.reduce_mean(local_loss), global_stats

    def log_prob_iterator(iterator, is_training, reuse=False):
        if hps.direct_iterator and iterator is not None:
            x, y = iterator.get_next()
        else:
            x, y = X, Y

        probs = log_prob(x, y, reuse)

        print(probs.shape)

        auc_score = tf.metrics.auc(y, tf.exp(probs))

        #auc = roc_auc_score(y, probs.flatten())

        stats = [auc_score]
        global_stats = Z.allreduce_mean(
            tf.stack([tf.reduce_mean(i) for i in stats]))

        return tf.reduce_mean(probs), global_stats

    feeds = {'x': X, 'y': Y}
    m = abstract_model_xy(sess, hps, feeds, train_iterator,
                          test_iterator, data_init, lr, f_loss, log_prob_iterator=log_prob_iterator)

    # === Decoding functions
    m.eps_std = tf.placeholder(tf.float32, [None], name='eps_std')
    x_sampled = f_decode(Y, m.eps_std)

    def m_decode(_y, _eps_std):
        return m.sess.run(x_sampled, {Y: _y, m.eps_std: _eps_std})
    m.decode = m_decode

    log_prob_temp = log_prob(X, Y, True)

    def m_log_prob(_x, _y):
        return m.sess.run(log_prob_temp, {X: _x, Y: _y})
    m.log_prob = m_log_prob

    return m


def checkpoint(z, logdet):
    zshape = Z.int_shape(z)
    z = tf.reshape(z, [-1, zshape[1]*zshape[2]*zshape[3]])
    logdet = tf.reshape(logdet, [-1, 1])
    combined = tf.concat([z, logdet], axis=1)
    tf.add_to_collection('checkpoints', combined)
    logdet = combined[:, -1]
    z = tf.reshape(combined[:, :-1], [-1, zshape[1], zshape[2], zshape[3]])
    return z, logdet


@add_arg_scope
def revnet2d(name, z, logdet, hps, reverse=False):
    with tf.variable_scope(name):
        if not reverse:
            for i in range(hps.depth):
                z, logdet = checkpoint(z, logdet)
                z, logdet = revnet2d_step(str(i), z, logdet, hps, reverse)
            z, logdet = checkpoint(z, logdet)
        else:
            for i in reversed(range(hps.depth)):
                z, logdet = revnet2d_step(str(i), z, logdet, hps, reverse)
    return z, logdet

# Simpler, new version


@add_arg_scope
def revnet2d_step(name, z, logdet, hps, reverse):
    with tf.variable_scope(name):

        shape = Z.int_shape(z)
        n_z = shape[3]
        assert n_z % 2 == 0

        if not reverse:

            z, logdet = Z.actnorm("actnorm", z, logdet=logdet)

            if hps.flow_permutation == 0:
                z = Z.reverse_features("reverse", z)
            elif hps.flow_permutation == 1:
                z = Z.shuffle_features("shuffle", z)
            elif hps.flow_permutation == 2:
                z, logdet = invertible_1x1_conv("invconv", z, logdet)
            else:
                raise Exception()

            z1 = z[:, :, :, :n_z // 2]
            z2 = z[:, :, :, n_z // 2:]

            if hps.flow_coupling == 0:
                z2 += f("f1", z1, hps.width)
            elif hps.flow_coupling == 1:
                h = f("f1", z1, hps.width, n_z)
                shift = h[:, :, :, 0::2]
                #scale = tf.exp(h[:, :, :, 1::2])
                scale = tf.nn.sigmoid(h[:, :, :, 1::2] + 2.)
                z2 += shift
                z2 *= scale
                logdet += tf.reduce_sum(tf.log(scale), axis=[1, 2, 3])
            else:
                raise Exception()

            z = tf.concat([z1, z2], 3)

        else:

            z1 = z[:, :, :, :n_z // 2]
            z2 = z[:, :, :, n_z // 2:]

            if hps.flow_coupling == 0:
                z2 -= f("f1", z1, hps.width)
            elif hps.flow_coupling == 1:
                h = f("f1", z1, hps.width, n_z)
                shift = h[:, :, :, 0::2]
                #scale = tf.exp(h[:, :, :, 1::2])
                scale = tf.nn.sigmoid(h[:, :, :, 1::2] + 2.)
                z2 /= scale
                z2 -= shift
                logdet -= tf.reduce_sum(tf.log(scale), axis=[1, 2, 3])
            else:
                raise Exception()

            z = tf.concat([z1, z2], 3)

            if hps.flow_permutation == 0:
                z = Z.reverse_features("reverse", z, reverse=True)
            elif hps.flow_permutation == 1:
                z = Z.shuffle_features("shuffle", z, reverse=True)
            elif hps.flow_permutation == 2:
                z, logdet = invertible_1x1_conv(
                    "invconv", z, logdet, reverse=True)
            else:
                raise Exception()

            z, logdet = Z.actnorm("actnorm", z, logdet=logdet, reverse=True)

    return z, logdet


def f(name, h, width, n_out=None):
    n_out = n_out or int(h.get_shape()[3])
    with tf.variable_scope(name):
        h = tf.nn.relu(Z.conv2d("l_1", h, width))
        h = tf.nn.relu(Z.conv2d("l_2", h, width, filter_size=[1, 1]))
        h = Z.conv2d_zeros("l_last", h, n_out)
    return h


def f_resnet(name, h, width, n_out=None):
    n_out = n_out or int(h.get_shape()[3])
    with tf.variable_scope(name):
        h = tf.nn.relu(Z.conv2d("l_1", h, width))
        h = Z.conv2d_zeros("l_2", h, n_out)
    return h

# Invertible 1x1 conv


@add_arg_scope
def invertible_1x1_conv(name, z, logdet, reverse=False):

    if True:  # Set to "False" to use the LU-decomposed version

        with tf.variable_scope(name):

            shape = Z.int_shape(z)
            w_shape = [shape[3], shape[3]]

            # Sample a random orthogonal matrix:
            w_init = np.linalg.qr(np.random.randn(
                *w_shape))[0].astype('float32')

            w = tf.get_variable("W", dtype=tf.float32, initializer=w_init)

            #dlogdet = tf.linalg.LinearOperator(w).log_abs_determinant() * shape[1]*shape[2]
            dlogdet = tf.cast(tf.log(abs(tf.matrix_determinant(
                tf.cast(w, 'float64')))), 'float32') * shape[1]*shape[2]

            if not reverse:

                _w = tf.reshape(w, [1, 1] + w_shape)
                z = tf.nn.conv2d(z, _w, [1, 1, 1, 1],
                                 'SAME', data_format='NHWC')
                logdet += dlogdet

                return z, logdet
            else:

                _w = tf.matrix_inverse(w)
                _w = tf.reshape(_w, [1, 1]+w_shape)
                z = tf.nn.conv2d(z, _w, [1, 1, 1, 1],
                                 'SAME', data_format='NHWC')
                logdet -= dlogdet

                return z, logdet

    else:

        # LU-decomposed version
        shape = Z.int_shape(z)
        with tf.variable_scope(name):

            dtype = 'float64'

            # Random orthogonal matrix:
            import scipy
            np_w = scipy.linalg.qr(np.random.randn(shape[3], shape[3]))[
                0].astype('float32')

            np_p, np_l, np_u = scipy.linalg.lu(np_w)
            np_s = np.diag(np_u)
            np_sign_s = np.sign(np_s)
            np_log_s = np.log(abs(np_s))
            np_u = np.triu(np_u, k=1)

            p = tf.get_variable("P", initializer=np_p, trainable=False)
            l = tf.get_variable("L", initializer=np_l)
            sign_s = tf.get_variable(
                "sign_S", initializer=np_sign_s, trainable=False)
            log_s = tf.get_variable("log_S", initializer=np_log_s)
            #S = tf.get_variable("S", initializer=np_s)
            u = tf.get_variable("U", initializer=np_u)

            p = tf.cast(p, dtype)
            l = tf.cast(l, dtype)
            sign_s = tf.cast(sign_s, dtype)
            log_s = tf.cast(log_s, dtype)
            u = tf.cast(u, dtype)

            w_shape = [shape[3], shape[3]]

            l_mask = np.tril(np.ones(w_shape, dtype=dtype), -1)
            l = l * l_mask + tf.eye(*w_shape, dtype=dtype)
            u = u * np.transpose(l_mask) + tf.diag(sign_s * tf.exp(log_s))
            w = tf.matmul(p, tf.matmul(l, u))

            if True:
                u_inv = tf.matrix_inverse(u)
                l_inv = tf.matrix_inverse(l)
                p_inv = tf.matrix_inverse(p)
                w_inv = tf.matmul(u_inv, tf.matmul(l_inv, p_inv))
            else:
                w_inv = tf.matrix_inverse(w)

            w = tf.cast(w, tf.float32)
            w_inv = tf.cast(w_inv, tf.float32)
            log_s = tf.cast(log_s, tf.float32)

            if not reverse:

                w = tf.reshape(w, [1, 1] + w_shape)
                z = tf.nn.conv2d(z, w, [1, 1, 1, 1],
                                 'SAME', data_format='NHWC')
                logdet += tf.reduce_sum(log_s) * (shape[1]*shape[2])

                return z, logdet
            else:

                w_inv = tf.reshape(w_inv, [1, 1]+w_shape)
                z = tf.nn.conv2d(
                    z, w_inv, [1, 1, 1, 1], 'SAME', data_format='NHWC')
                logdet -= tf.reduce_sum(log_s) * (shape[1]*shape[2])

                return z, logdet


@add_arg_scope
def split2d(name, z, objective=0.):
    with tf.variable_scope(name):
        n_z = Z.int_shape(z)[3]
        z1 = z[:, :, :, :n_z // 2]
        z2 = z[:, :, :, n_z // 2:]
        pz = split2d_prior(z1)
        objective += pz.logp(z2)
        z1 = Z.squeeze2d(z1)
        return z1, objective


@add_arg_scope
def split2d_reverse(name, z, eps_std=None):
    with tf.variable_scope(name):
        z1 = Z.unsqueeze2d(z)
        pz = split2d_prior(z1)
        z2 = pz.sample
        if eps_std is not None:
            z2 = pz.sample2(pz.eps * tf.reshape(eps_std, [-1, 1, 1, 1]))
        z = tf.concat([z1, z2], 3)
        return z


@add_arg_scope
def split2d_prior(z):
    n_z2 = int(z.get_shape()[3])
    n_z1 = n_z2
    h = Z.conv2d_zeros("conv", z, 2 * n_z1)

    mean = h[:, :, :, 0::2]
    logs = h[:, :, :, 1::2]
    return Z.gaussian_diag(mean, logs)

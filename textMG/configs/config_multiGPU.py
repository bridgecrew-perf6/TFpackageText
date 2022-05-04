import tensorflow as tf

from textMG.configs.config import args
from textMG.datasets.dataset import Dataset
from textMG.datasets.generator import Generator
from textMG.utils.loss import loss_function
from textMG.utils.loggers import logger

def multi_GPU_training(conv_net):
    tower_grads = []
    reuse_vars = False
    loss_patch_list = []
    """
    # load data to memory first then iterator
    # initilize placeholder
    X, Y = placeholder_init(args.num_input, args.num_classes)
    # # create an input of network and iterator
    X_batch_numGPU, Y_batch_numGPU, iterator = iterator_to_patch(X, Y, args.num_input, args.num_classes)
    """
    # load data from disk and generator.
    generator = Generator()

    # print 2 outputs from our generator just to see if it works:
    iter = generator.get_next_patch(batch=2)
    for i in range(2):
        el = next(iter)
        logger.debug("input shape: {}; output shape: {}".format([len(el['x_input']), len(el['x_input'][0])], [len(el['y_output'])]))
    X_batch_numGPU, Y_batch_numGPU, train_initializer = build_iterator(generator)
    # optimizer init
    optimizer = tf.train.AdamOptimizer(learning_rate=args.learning_rate)

    # Loop over all GPUs and construct their own computation graph
    for i in range(args.num_gpusORcpus):
        with tf.device(assign_to_device('/{}:{}'.format(args.device_type.lower(), i), ps_device='/cpu:0')):
            print("starting {}:".format(args.device_type), i)
            # Split data between GPUs
            _x = X_batch_numGPU[i * args.batch_size: (i + 1) * args.batch_size]
            _y = Y_batch_numGPU[i * args.batch_size: (i + 1) * args.batch_size]

            # Because Dropout have different behavior at training and prediction time, we
            # need to create 2 distinct computation graphs that share the same weights.

            # Create a graph for training
            outputs_train = conv_net(_x, args.num_classes, args.dropout,
                                     reuse=reuse_vars, is_training=True)
            pred_train = outputs_train['output']
            # Create another graph for testing that reuse the same weights
            outputs_test = conv_net(_x, args.num_classes, args.dropout,
                                    reuse=True, is_training=False)
            pred_test = outputs_test['output']

            # Define loss and optimizer (with train logits, for dropout to take effect)
            loss_op = loss_function(pred_train, _y)
            loss_patch_list.append(loss_op)
            grads = optimizer.compute_gradients(loss_op)

            # Only first GPU compute accuracy
            # if i == 0:
            #	# Evaluate model (with test logits, for dropout to be disabled)
            #	accuracy = accuracy_cal(pred_test, _y)

            reuse_vars = True
            tower_grads.append(grads)

    ave_tower_grads = average_gradients(tower_grads)
    train_op = optimizer.apply_gradients(ave_tower_grads)
    loss_patch_ave = tf.reduce_mean(tf.cast(tf.stack(loss_patch_list), tf.float32))
    return train_op, loss_patch_ave, train_initializer, X_batch_numGPU, Y_batch_numGPU

def build_iterator(generator):
    logger.info("load the data from tf builtin 'tf.data.Dataset.from_generator' ")
    with tf.device('/cpu:1'):
        train_dataset = tf.data.Dataset.from_generator(generator.get_next_patch,
                                                       output_types={generator.input: tf.float32,
                                                                     generator.output: tf.float32})
        train_dataset = train_dataset.repeat().batch(args.batch_size * args.num_gpusORcpus).prefetch(1)
        # test_dataset = (Dataset('test').data_init()[0], Dataset('test').data_init()[1])
        # Create an iterator over the dataset
        iterator = tf.data.Iterator.from_structure(train_dataset.output_types, train_dataset.output_shapes)
        train_initializer = iterator.make_initializer(train_dataset)
        # iterator = train_dataset.make_initializable_iterator()
        elements_dict = iterator.get_next()
        return elements_dict['x_input'], elements_dict['y_output'], train_initializer

# site for set logical device
# https://colab.research.google.com/github/tensorflow/docs/blob/master/site/en/guide/gpu.ipynb#scrollTo=AqPo9ltUA_EY
def device_set(device_type, memory_limit=None):
    logger.info("use {} to train model".format(device_type))
    memory_limit = None if device_type == "CPU" else 1024
    gpuORcpu = tf.config.experimental.list_physical_devices(device_type)
    if gpuORcpu:
        # Create 2 virtual GPUs with 1GB memory each
        try:
            tf.config.experimental.set_virtual_device_configuration(
                gpuORcpu[0],
                [tf.config.experimental.VirtualDeviceConfiguration(memory_limit=memory_limit) for _ in
                 range(args.num_gpusORcpus)])
            logical_device = tf.config.experimental.list_logical_devices(device_type)
            logger.info("Physical {0}:{1} ; Logical {2}:{3}".format(device_type, len(gpuORcpu), device_type,
                                                              len(logical_device)))
        except RuntimeError as e:
            # Virtual devices must be set before GPUs have been initialized
            logger.critical("RuntimeError occurred", exc_info=1)
    else:
        logger.critical("Error: there is no physical device: {}".format(gpuORcpu))

# Build the function to average the gradients
def average_gradients(tower_grads):
    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        # Note that each grad_and_vars looks like the following:
        #   ((grad0_gpu0, var0_gpu0), ... , (grad0_gpuN, var0_gpuN))
        grads = []
        for g, _ in grad_and_vars:
            # Add 0 dimension to the gradients to represent the tower.
            expanded_g = tf.expand_dims(g, 0)

            # Append on a 'tower' dimension which we will average over below.
            grads.append(expanded_g)

        # Average over the 'tower' dimension.
        grad = tf.concat(grads, 0)
        grad = tf.reduce_mean(grad, 0)

        # Keep in mind that the Variables are redundant because they are shared
        # across towers. So .. we will just return the first tower's pointer to
        # the Variable.
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads

# By default, all variables will be placed on '/gpu:0'
# So we need a custom device function, to assign all variables to '/cpu:0'
# Note: If GPUs are peered, '/gpu:0' can be a faster option

PS_OPS = ['Variable', 'VariableV2', 'AutoReloadVariable']

def assign_to_device(device, ps_device='/cpu:0'):
    def _assign(op):
        node_def = op if isinstance(op, tf.NodeDef) else op.node_def
        if node_def.op in PS_OPS:
            return "/" + ps_device
        else:
            return device

    return _assign

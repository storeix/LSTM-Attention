from blocks.bricks import Initializable, Tanh, Rectifier
from blocks.bricks.base import application, lazy
from blocks.roles import add_role, WEIGHT, BIAS, INITIAL_STATE
from blocks.utils import shared_floatx_nans, shared_floatx_zeros
from blocks.bricks.recurrent import BaseRecurrent, recurrent
from theano.tensor.signal.downsample import max_pool_2d
from theano.sandbox.cuda.dnn import dnn_conv
import theano.tensor as tensor
import numpy as np
from crop import LocallySoftRectangularCropper
from crop import Gaussian


class LSTMAttention(BaseRecurrent, Initializable):
    @lazy(allocation=['dim'])
    def __init__(self, dim, mlp_hidden_dims, batch_size,
                 image_shape, patch_shape, activation=None, **kwargs):
        super(LSTMAttention, self).__init__(**kwargs)
        self.dim = dim
        conv_layers = [['conv_1_1', (64, 3, 3, 3), None, None],
                       ['conv_1_2', (64, 64, 3, 3), (2, 2), (1, 1)],
                       ['conv_2_1', (128, 64, 3, 3), None, None],
                       ['conv_2_2', (128, 128, 3, 3), (2, 2), (1, 1)],
                       ['conv_3_1', (256, 128, 3, 3), None, None],
                       ['conv_3_2', (256, 256, 3, 3), None, None],
                       ['conv_3_3', (256, 256, 3, 3), (2, 2), (1, 1)],
                       ['conv_4_1', (512, 256, 3, 3), None, None],
                       ['conv_4_2', (512, 512, 3, 3), None, None],
                       ['conv_4_3', (512, 512, 3, 3), (2, 2), (1, 1)],
                       ['conv_5_1', (512, 512, 3, 3), None, None],
                       ['conv_5_2', (512, 512, 3, 3), None, None],
                       ['conv_5_3', (512, 512, 3, 3), (2, 2), (1, 1)]]
        fc_layers = [['fc6', (25088, 4096)],
                     ['fc7', (4096, 4096)],
                     ['fc8-1', (4096, 101)]]

        conv_layers = [['conv_1', (16, 1, 5, 5), (2, 2), (2, 2)],
                       ['conv_2', (32, 16, 5, 5), None, None],
                       ['conv_3', (48, 32, 3, 3), (2, 2), (2, 2)]]
        fc_layers = [['fc4', (192, 256), 'relu']]
        conv_layers = []
        fc_layers = [['fc4', (576, self.dim), 'relu']]
        self.mlp_hidden_dims = mlp_hidden_dims
        self.conv_layers = conv_layers
        self.fc_layers = fc_layers
        self.image_shape = image_shape
        self.patch_shape = patch_shape
        self.batch_size = batch_size
        cropper = LocallySoftRectangularCropper(
            patch_shape=patch_shape,
            hyperparameters={'cutoff': 3, 'batched_window': True},
            kernel=Gaussian())
        self.rescaling_factor = float(patch_shape[0]) / float(image_shape[0])
        self.min_scale = 0.24 - 0.05
        # self.rescaling_factor = 0.0

        if not activation:
            activation = Tanh()
        self.children = [activation, cropper, Tanh(), Rectifier()]

    def get_dim(self, name):
        if name == 'inputs':
            return self.dim * 4
        if name in ['states', 'cells']:
            return self.dim
        if name in ['location', 'scale']:
            return 2
        if name == 'mask':
            return 0
        if name == 'patch':
            return np.prod(self.patch_shape)
        if name == 'downn_sampled_input':
            return np.prod(self.patch_shape)
        return super(LSTMAttention, self).get_dim(name)

    def apply_attention_mlp(self, x):
        tanh = self.children[2].apply
        relu = self.children[3].apply
        pre_1 = tensor.dot(x, self.w_1_mlp) + self.b_1_mlp
        act_1 = relu(pre_1)
        pre_2 = (tensor.dot(act_1, self.w_2_mlp) + self.b_2_mlp +
                 np.asarray([0, 0, -1.0], dtype='float32'))
        act_2 = tanh(pre_2)
        return act_2

    def apply_conv(self, x):
        out = x
        relu = self.children[3].apply
        for layer in self.conv_layers:
            name, _, pool_shape, pool_stride = layer
            w = {w.name: w for w in self.conv_ws}[name + '_w']
            b = {b.name: b for b in self.conv_bs}[name + '_b']
            conv = dnn_conv(out, w)
            m = conv.mean(0, keepdims=True)
            s = conv.var(0, keepdims=True)
            # conv = (conv - m) / tensor.sqrt(s + np.float32(1e-8))
            conv = conv + b.dimshuffle('x', 0, 'x', 'x')
            out = relu(conv)
            if pool_shape is not None:
                out = max_pool_2d(
                    out, pool_shape, st=pool_stride, ignore_border=True)
        return out

    def apply_fc(self, x):
        out = x
        for layer in self.fc_layers:
            name, shape, act = layer
            w = {w.name: w for w in self.fc_ws}[name + '_w']
            b = {b.name: b for b in self.fc_bs}[name + '_b']
            if act == 'relu':
                act = self.children[3].apply
            elif act == 'tanh':
                act = self.children[2].apply
            elif act == 'lin':
                act = lambda n: n
            out = tensor.dot(out, w)
            m = out.mean(0, keepdims=True)
            s = out.var(0, keepdims=True)
            # out = (out - m) / tensor.sqrt(s + np.float32(1e-8))
            out = act(out + b)
        return out

    def _allocate(self):
        self.conv_ws = []
        self.conv_bs = []
        for layer in self.conv_layers:
            name, filter_shape, pool_shape, pool_stride = layer
            self.conv_ws.append(shared_floatx_nans(
                filter_shape, name=name + '_w'))
            self.conv_bs.append(shared_floatx_nans(
                (filter_shape[0],), name=name + '_b'))
        self.fc_ws = []
        self.fc_bs = []
        for layer in self.fc_layers:
            name, shape, act = layer
            self.fc_ws.append(shared_floatx_nans(
                shape, name=name + '_w'))
            self.fc_bs.append(shared_floatx_nans(
                (shape[1],), name=name + '_b'))
        self.b_1_mlp = shared_floatx_nans(
            (self.mlp_hidden_dims[0],), name='b_1_mlp')
        self.b_2_mlp = shared_floatx_nans(
            (self.mlp_hidden_dims[1],), name='b_2_mlp')
        self.w_1_mlp = shared_floatx_nans(
            (np.prod(self.patch_shape) + self.dim + 3,
                self.mlp_hidden_dims[0]), name='w_1_mlp')
        self.w_2_mlp = shared_floatx_nans(
            (self.mlp_hidden_dims[0],
                self.mlp_hidden_dims[1]), name='w_2_mlp')
        self.W_pre_lstm = shared_floatx_nans((self.dim + 3, 4 * self.dim),
                                             name='W_pre_lstm')
        self.b_pre_lstm = shared_floatx_nans((4 * self.dim,),
                                             name='b_pre_lstm')
        self.W_state = shared_floatx_nans((self.dim, 4 * self.dim),
                                          name='W_state')
        self.initial_state_ = shared_floatx_zeros((self.dim,),
                                                  name="initial_state")
        self.initial_cells = shared_floatx_zeros((self.dim,),
                                                 name="initial_cells")
        self.initial_location = shared_floatx_zeros((2,),
                                                    name="initial_location")
        self.initial_scale = shared_floatx_zeros((1,),
                                                 name="initial_scale")
        add_role(self.W_state, WEIGHT)
        add_role(self.W_pre_lstm, WEIGHT)
        add_role(self.b_pre_lstm, BIAS)
        add_role(self.b_1_mlp, BIAS)
        add_role(self.b_2_mlp, BIAS)
        add_role(self.w_1_mlp, WEIGHT)
        add_role(self.w_2_mlp, WEIGHT)
        add_role(self.initial_state_, INITIAL_STATE)
        add_role(self.initial_cells, INITIAL_STATE)
        add_role(self.initial_location, INITIAL_STATE)
        add_role(self.initial_scale, INITIAL_STATE)
        for w in self.conv_ws + self.fc_ws:
            add_role(w, WEIGHT)
        for b in self.conv_bs + self.fc_bs:
            add_role(b, BIAS)

        self.parameters = [
            self.W_state, self.W_pre_lstm, self.w_1_mlp, self.w_2_mlp,
            self.b_pre_lstm, self.b_1_mlp, self.b_2_mlp, self.initial_state_,
            self.initial_cells, self.initial_location,
            self.initial_scale] +\
            self.conv_ws + self.conv_bs +\
            self.fc_ws + self.fc_bs

    def _initialize(self):
        for weights in self.parameters[:4] + self.conv_ws + self.fc_ws:
            self.weights_init.initialize(weights, self.rng)
        for biases in self.parameters[4:7] + self.conv_bs + self.fc_bs:
            self.biases_init.initialize(biases, self.rng)

    @recurrent(sequences=['inputs', 'mask'],
               states=['states', 'cells', 'location', 'scale'],
               contexts=[],
               outputs=['states', 'cells', 'location', 'scale',
                        'patch', 'downn_sampled_input'])
    def apply(self, inputs, states, location, scale, cells, mask=None):
        def slice_last(x, no):
            return x[:, no * self.dim: (no + 1) * self.dim]

        nonlinearity = self.children[0].apply
        cropper = self.children[1]

        downn_sampled_input = cropper.apply(
            inputs.reshape((self.batch_size, 1,) + self.image_shape),
            np.array([list(self.image_shape)]),
            tensor.constant(
                (self.batch_size *
                    [[self.image_shape[0] / 2,
                     self.image_shape[1] / 2]])).astype('float32'),
            tensor.constant(self.batch_size *
                            [[self.rescaling_factor, ] * 2]).astype('float32'))
        flat_downn_sampled_input = downn_sampled_input.flatten(ndim=2)

        mlp_output = self.apply_attention_mlp(tensor.concatenate(
            [flat_downn_sampled_input, location, scale, states], axis=1))
        location = mlp_output[:, 0:2]
        location.name = 'location'
        scale = mlp_output[:, 2:3]
        scale.name = 'scale'

        patch = cropper.apply(
            inputs.reshape((self.batch_size, 1,) + self.image_shape),
            np.array([list(self.image_shape)]),
            (location + 1) * self.image_shape[0] / 2,
            # use the same scale for both x and y
            tensor.concatenate([
                scale + 1 + self.min_scale,
                scale + 1 + self.min_scale], axis=1))

        # It is a nice name, isn't it?
        # B x C x X x Y
        conved_patch = self.apply_conv(patch)
        flat_conved_patch = conved_patch.flatten(2)
        pre_lstm = self.apply_fc(flat_conved_patch)
        pre_lstm = tensor.concatenate(
            [pre_lstm, location, scale], axis=1)
        transformed_pre_lstm = tensor.dot(
            pre_lstm, self.W_pre_lstm) + self.b_pre_lstm

        activation = tensor.dot(states, self.W_state) + transformed_pre_lstm
        in_gate = tensor.nnet.sigmoid(slice_last(activation, 0))
        forget_gate_input = slice_last(activation, 1)
        forget_gate = tensor.nnet.sigmoid(forget_gate_input +
                                          tensor.ones_like(forget_gate_input))
        next_cells = (forget_gate * cells +
                      in_gate * nonlinearity(slice_last(activation, 2)))
        out_gate = tensor.nnet.sigmoid(slice_last(activation, 3))
        next_states = out_gate * nonlinearity(next_cells)

        if mask:
            next_states = (mask[:, None] * next_states +
                           (1 - mask[:, None]) * states)
            next_cells = (mask[:, None] * next_cells +
                          (1 - mask[:, None]) * cells)

        return (next_states, next_cells, location, scale,
                patch, downn_sampled_input)

    @application(outputs=apply.states)
    def initial_states(self, batch_size, *args, **kwargs):
        return [tensor.repeat(self.initial_state_[None, :], batch_size, 0),
                tensor.repeat(self.initial_cells[None, :], batch_size, 0),
                tensor.repeat(self.initial_location[None, :], batch_size, 0),
                tensor.repeat(self.initial_scale[None, :], batch_size, 0)]

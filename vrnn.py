import torch
import functools
import torch.utils
import torch.utils.data
import torch.nn as nn
import torch.nn.functional as F
import numpy as np

from copy import deepcopy

from .abstract_vae import AbstractVAE
from .reparameterizers.gumbel import GumbelSoftmax
from .reparameterizers.mixture import Mixture
from .reparameterizers.beta import Beta
from .reparameterizers.isotropic_gaussian import IsotropicGaussian
from helpers.distributions import nll_activation as nll_activation_fn
from helpers.distributions import nll as nll_fn
from helpers.layers import get_encoder, get_decoder, Identity, EMA
from helpers.utils import eps as eps_fn, add_noise_to_imgs, float_type
from helpers.utils import same_type, zeros_like, expand_dims, \
    zeros, nan_check_and_break

class VRNNMemory(nn.Module):
    def __init__(self, h_dim, n_layers, bidirectional,
                 config, rnn=None, cuda=False):
        """  Helper object to abstract away memory for VRNN.

        :param h_dim: hidden size
        :param n_layers: number of layers for RNN
        :param bidirectional: bidirectional bool flag
        :param config: argparse
        :param rnn: the rnn object
        :param cuda: cuda flag
        :returns: VRNNMemory object
        :rtype: nn.Module

        """
        super(VRNNMemory, self).__init__()
        self.model = rnn
        self.config = config
        self.n_layers = n_layers
        self.bidirectional = bidirectional
        self.h_dim = h_dim
        self.use_cuda = cuda
        self.memory_buffer = []

    @staticmethod
    def _state_from_tuple(tpl):
        """ Return the state from the tuple.

        :param tpl: the state-output tuple.
        :returns: state
        :rtype: torch.Tensor

        """
        _, state = tpl
        return state

    @staticmethod
    def _output_from_tuple(tpl):
        """ Return the output from the tuple.

        :param tpl: the state-output tuple.
        :returns: output.
        :rtype: torch.Tensor

        """
        output, _ = tpl
        return output

    def _append_to_buffer(self, tpl):
        """ Append the tuple to the running memory buffer.

        :param tpl: the current tuple
        :returns: None
        :rtype: None

        """
        output_t, state_t = tpl
        self.memory_buffer.append([output_t.clone(), (state_t[0].clone(),
                                                      state_t[1].clone())])

    def clear(self):
        """ Clears the memory.

        :returns: None
        :rtype: None

        """
        self.memory_buffer.clear()

    def init_state(self, batch_size, cuda=False,
                   override_noisy_state=False):
        """ Initializes (or re-initializes) the state.

        :param batch_size: size of batch
        :param cuda: bool flag
        :param override_noisy_state: bool flag for setting up noisy state
        :returns: state-tuple
        :rtype: (torch.Tensor, torch.Tensor)

        """
        def _init(batch_size, cuda):
            """ Return a single initialized state

            :param batch_size: batch size
            :param cuda: is on cuda or not
            :returns: a single state init
            :rtype: (torch.Tensor, torch.Tensor)

            """
            num_directions = 2 if self.bidirectional else 1
            if override_noisy_state or \
               (self.training and self.config['use_noisy_rnn_state']):
                # add some noise to initial state
                # consider also: nn.init.xavier_uniform_(
                return same_type(self.config['half'], cuda)(
                    num_directions * self.n_layers, batch_size, self.h_dim
                ).normal_(0, 0.01).requires_grad_()


            # return zeros for testing
            return same_type(self.config['half'], cuda)(
                num_directions * self.n_layers, batch_size, self.h_dim
            ).zero_().requires_grad_()

        self.state = ( # LSTM state is (h, c)
            _init(batch_size, cuda),
            _init(batch_size, cuda)
        )

    def update(self, tpl):
        """ Adds tuple to buffer and set current members

        :param tpl: the state-output tuple.
        :returns: None
        :rtype: None

        """
        self._append_to_buffer(tpl)
        self.outputs, self.state = tpl

    def forward(self, input_t, reset_state=False):
        """ Single-step forward pass, encodes with RNN and cache state.

        :param input_t: the current input tensor
        :param reset_state: whether or not to reset the current state
        :returns: the current output
        :rtype: torch.Tensor

        """
        batch_size = input_t.size(0)
        if reset_state:
            self.init_state(batch_size, input_t.is_cuda)

        input_t = input_t.contiguous()

        # if not self.config['half']:
        self.update(self.model(input_t, self.state))
        # else:
        # self.update(self.model(input_t, collect_hidden=True))

        return self.get_output()

    def get_state(self):
        """ Returns latest state.

        :returns: state
        :rtype: torch.Tensor

        """
        assert hasattr(self, 'state'), "do a forward pass first"
        return self.state

    def get_repackaged_state(self, h=None):
        """ Wraps hidden states in new Tensors, to detach them from their history.

        :param h: the state tuple to repackage (optional).
        :returns: tuple of repackaged states
        :rtype: (torch.Tensor, torch.Tensor)

        """
        if h is None:
            return self.get_repackaged_state(self.state)

        if isinstance(h, torch.Tensor):
            return h.detach()

        return tuple(self.get_repackaged_state(v) for v in h)

    def get_output(self):
        """ Helper to get the latest output

        :returns: output tensor
        :rtype: torch.Tensor

        """
        assert hasattr(self, 'outputs'), "do a forward pass first"
        return self.outputs

    def get_merged_memory(self):
        """ Merges over num_layers of the state which is [nlayer, batch, latent]

        :returns: merged temporal memory.
        :rtype: torch.Tensor

        """
        assert hasattr(self, 'memory_buffer'), "do a forward pass first"
        mem_concat = torch.cat([self._state_from_tuple(mem)[0]
                                for mem in self.memory_buffer], 0)
        return torch.mean(mem_concat, 0)

    def get_final_memory(self):
        """ Get the final memory state.

        :returns: the final memory state.
        :rtype: torch.Tensor

        """
        assert hasattr(self, 'memory_buffer'), "do a forward pass first"
        return self._state_from_tuple(self.memory_buffer[-1])[0]


class VRNN(AbstractVAE):
    def __init__(self, input_shape, n_layers=2, bidirectional=False, **kwargs):
        """ Implementation of the Variational Recurrent
            Neural Network (VRNN) from https://arxiv.org/abs/1506.02216

        :param input_shape: the input dimension
        :param n_layers: number of RNN / equivalent layers
        :param bidirectional: whether the model is bidirectional or not
        :returns: VRNN object
        :rtype: AbstractVAE

        """
        super(VRNN, self).__init__(input_shape, **kwargs)
        self.bidirectional = bidirectional
        self.n_layers = n_layers

        # build the reparameterizer
        if self.config['reparam_type'] == "isotropic_gaussian":
            print("using isotropic gaussian reparameterizer")
            self.reparameterizer = IsotropicGaussian(self.config)
        elif self.config['reparam_type'] == "discrete":
            print("using gumbel softmax reparameterizer")
            self.reparameterizer = GumbelSoftmax(self.config)
        elif self.config['reparam_type'] == "beta":
            print("using beta reparameterizer")
            self.reparameterizer = Beta(self.config)
        elif "mixture" in self.config['reparam_type']:
            print("using mixture reparameterizer with {} + discrete".format(
                'beta' if 'beta' in self.config['reparam_type'] else 'isotropic_gaussian'
            ))
            self.reparameterizer = Mixture(num_discrete=self.config['discrete_size'],
                                           num_continuous=self.config['continuous_size'],
                                           config=self.config,
                                           is_beta='beta' in self.config['reparam_type'])
        else:
            raise Exception("unknown reparameterization type")

        # keep track of ammortized posterior
        self.aggregate_posterior = nn.ModuleDict({
            'encoder_logits': EMA(0.999),
            'prior_logits': EMA(0.999)
        })

        # build the entire model
        self._build_model()

    def _build_phi_x_model(self):
        """ simple helper to build the feature extractor for x

        :returns: a model for phi_x
        :rtype: nn.Module

        """
        return self._lazy_build_phi_x(self.input_shape)

    def _lazy_build_phi_x(self, input_shape):
        """ Lazily build an encoder to extract features.

        :param input_shape: the input tensor shape
        :returns: an encoder module
        :rtype: nn.Module

        """
        return nn.Sequential(
            get_encoder(self.config)(input_shape=input_shape,
                                     output_size=self.config['latent_size'],
                                     activation_fn=self.activation_fn),
            self.activation_fn()
            #nn.SELU()
        )

    def _lazy_rnn_lambda(self, x, state,
                          model_type='lstm',
                          bias=True,
                          dropout=0):
        """ automagically builds[if it does not exist]
            and returns the output of an RNN lazily

        :param x: the input tensor
        :param state: the state tensor
        :param model_type: lstm or gru
        :param bias: whether to use a bias or not
        :param dropout: whether to use dropout or not
        :returns: lazy-inits an RNN and returns the RNN forward pass
        :rtype: (torch.Tensor, torch.Tensor)

        """
        if not hasattr(self, 'rnn'):
            self.rnn = self._build_rnn_memory_model(input_size=x.size(-1),
                                                    model_type=model_type,
                                                    bias=bias,
                                                    dropout=dropout)

        return self.rnn(x, state)

    def _get_dense_net_map(self, name='vrnn'):
        """ helper to pull a dense encoder

        :param name: the name of the dense network
        :returns: the dense network
        :rtype: nn.Module

        """
        config = deepcopy(self.config)
        config['encoder_layer_type'] = 'dense'
        return get_encoder(config, name=name)

    def _build_model(self):
        """ Helper to build the entire model as members of this class.

        :returns: None
        :rtype: None

        """
        input_size = int(np.prod(self.input_shape))

        # feature-extracting transformations
        self.phi_x = self._build_phi_x_model()
        self.phi_x_i = []
        self.phi_z = nn.Sequential(
            self._get_dense_net_map('phi_z')(
                self.reparameterizer.output_size, self.config['latent_size'],
                activation_fn=self.activation_fn,
                normalization_str=self.config['dense_normalization'],
                #activation_fn=Identity,     # XXX: hardcode
                #normalization_str='batchnorm',     # XXX: hardcode
                num_layers=2
            ),
            nn.SELU()
            # self.activation_fn()
        )

        # prior
        self.prior = self._get_dense_net_map('prior')(
            self.config['latent_size'], self.reparameterizer.input_size,
            activation_fn=self.activation_fn,
            normalization_str=self.config['dense_normalization'],
            # activation_fn=Identity,
            # normalization_str='batchnorm',
            num_layers=2
        )

        # decoder
        self.decoder = self.build_decoder()

        # memory module that contains the RNN or DNC
        self.memory = VRNNMemory(h_dim=self.config['latent_size'],
                                 n_layers=self.n_layers,
                                 bidirectional=self.bidirectional,
                                 config=self.config,
                                 rnn=self._lazy_rnn_lambda,
                                 cuda=self.config['cuda'])

    def build_decoder(self, reupsample=True):
        """ helper function to build convolutional or dense decoder

        :returns: a decoder
        :rtype: nn.Module

        """
        if self.config['decoder_layer_type'] == "pixelcnn":
            assert self.config['nll_type'] == "disc_mix_logistic", \
                "pixelcnn only works with disc_mix_logistic"

        decoder = get_decoder(self.config, reupsample)(input_size=self.config['latent_size']*2,
                                                       output_shape=self.input_shape,
                                                       activation_fn=self.activation_fn,
                                                       reupsample=True)
        # append the variance as necessary
        return self._append_variance_projection(decoder)

    def fp16(self):
        """ Helper to convert to FP16 model.

        :returns: None
        :rtype: None

        """
        self.phi_x = self.phi_x.half()
        self.phi_z = self.phi_z.half()
        self.prior = self.prior.half()
        super(VRNN, self).fp16()
        # RNN should already be half'd

    def parallel(self):
        """ Converts to data-parallel model

        :returns: None
        :rtype: None

        """
        self.phi_x = nn.DataParallel(self.phi_x)
        self.phi_z = nn.DataParallel(self.phi_z)
        self.prior = nn.DataParallel(self.prior)
        super(VRNN, self).parallel()

        # TODO: try to get this working
        #self.memory.model = nn.DataParallel(self.memory.model)

    def has_discrete(self):
        """ True is we have a discrete reparameterization

        :returns: True/False
        :rtype: bool

        """
        return self.config['reparam_type'] == 'mixture' \
            or self.config['reparam_type'] == 'discrete'

    def _build_rnn_memory_model(self, input_size, model_type='lstm', bias=True, dropout=0):
        """ Builds an RNN Memory Model. Currently restricted to LSTM.

        :param input_size:
        :param model_type:
        :param bias:
        :param dropout:
        :returns:
        :rtype:

        """
        if self.config['half']:
            import apex

        model_fn_map = {
            'gru': torch.nn.GRU if not self.config['half'] else apex.RNN.GRU,
            'lstm': torch.nn.LSTM if not self.config['half'] else apex.RNN.LSTM
        }
        rnn = model_fn_map[model_type](
            input_size=input_size,
            hidden_size=self.config['latent_size'],
            num_layers=self.n_layers,
            bidirectional=self.bidirectional,
            bias=bias, dropout=dropout
        )

        if self.config['cuda'] and not self.config['half']:
            rnn.flatten_parameters()

        return rnn

    def _clamp_variance(self, logits):
        """ clamp the variance when using a gaussian dist.

        :param logits: the un-activated logits
        :returns: the logits, clamped
        :rtype: torch.Tensor

        """
        if self.config['reparam_type'] == 'isotropic_gaussian':
            feat_size = logits.size(-1)
            return torch.cat(
                [logits[:, 0:feat_size//2],
                 torch.sigmoid(logits[:, feat_size//2:])],
                -1)
        elif self.config['reparam_type'] == 'mixture':
            feat_size = self.reparameterizer.num_continuous_input
            return torch.cat(
                [logits[:, 0:feat_size//2],                    # mean
                 torch.sigmoid(logits[:, feat_size//2:feat_size]), # clamped var
                 logits[:, feat_size:]],                       # discrete
                -1)
        else:
            return logits

    def reparameterize(self, logits_map):
        """ reparameterize the encoder output and the prior

        :param logits_map: the map of logits
        :returns: a dict of reparameterized things
        :rtype: dict

        """
        # nan_check_and_break(logits_map['encoder_logits'], "enc_logits")
        # nan_check_and_break(logits_map['prior_logits'], "prior_logits")
        z_enc_t, params_enc_t = self.reparameterizer(logits_map['encoder_logits'])

        # XXX: clamp the variance of gaussian priors to not explode
        logits_map['prior_logits'] = self._clamp_variance(logits_map['prior_logits'])

        # reparamterize the prior distribution
        z_prior_t, params_prior_t = self.reparameterizer(logits_map['prior_logits'])

        z = {  # reparameterization
            'prior': z_prior_t,
            'posterior': z_enc_t,
            'x_features': logits_map['x_features']
        }
        params = {  # params of the posterior
            'prior': params_prior_t,
            'posterior': params_enc_t
        }

        return z, params

    def forward(self, input_t):
        """ Multi-step forward pass for VRNN.

        :param input_t: input tensor or list of tensors
        :returns: final output tensor
        :rtype: torch.Tensor

        """
        decoded, params = [], []
        batch_size = input_t.shape[0] if isinstance(input_t, torch.Tensor) else input_t[0].shape[0]

        self.memory.init_state(batch_size, input_t.is_cuda) # always re-init state at first step.
        for i in range(self.config['max_time_steps']):
            if isinstance(input_t, list):  # if we have many inputs as a list
                decode_i, params_i = self.step(input_t[i])
            else:                          # single input encoded many times
                decode_i, params_i = self.step(input_t)
                input_t = decode_i if i == 0 else decode_i + input_t

            params_i = self._compute_mi_params(decode_i, params_i)

            decoded.append(decode_i)
            params.append(params_i)

        self.memory.clear()                # clear memory to prevent perennial growth
        return decoded, params

    def step(self, x_i, inference_only=False):
        """ Single step forward pass.

        :param x_related: input tensor
        :param inference_only:
        :returns:
        :rtype:

        """
        x_i_inference = add_noise_to_imgs(x_i) \
            if self.config['add_img_noise'] else x_i             # add image quantization noise
        z_t, params_t = self.posterior(x_i_inference)
        nan_check_and_break(x_i_inference, "x_related_inference")
        nan_check_and_break(z_t['prior'], "prior")
        nan_check_and_break(z_t['posterior'], "posterior")
        nan_check_and_break(z_t['x_features'], "x_features")

        # decode the posterior
        decoded_t = self.decode(z_t, produce_output=True)
        nan_check_and_break(decoded_t, "decoded_t")

        return decoded_t, params_t


    def decode(self, z_t, produce_output=False, reset_state=False):
        """ decodes using VRNN

        :param z_t: the latent sample
        :param produce_output: produce output or just update stae
        :param reset_state: reset the state of the RNN
        :returns: decoded logits
        :rtype: torch.Tensor

        """
        # grab state from RNN, TODO: evaluate recovery methods below
        # [0] grabs the h from LSTM (as opposed to (h, c))
        final_state = torch.mean(self.memory.get_state()[0], 0)
        # nan_check_and_break(final_state, "final_rnn_output[decode]")

        # feature transform for z_t
        phi_z_t = self.phi_z(z_t['posterior'])
        # nan_check_and_break(phi_z_t, "phi_z_t")

        # concat and run through RNN to update state
        input_t = torch.cat([z_t['x_features'], phi_z_t], -1).unsqueeze(0)
        self.memory(input_t.contiguous(), reset_state=reset_state)

        # decode only if flag is set
        dec_t = None
        if produce_output:
            dec_input_t = torch.cat([phi_z_t, final_state], -1)
            dec_t = self.decoder(dec_input_t)

        return dec_t

    def _extract_features(self, x, *xargs):
        """ accepts x and any number of extra x items and returns
            each of them projected through it's own NN,
            creating any networks as needed

        :param x: the input tensor
        :returns: the extracted features
        :rtype: torch.Tensor

        """
        phi_x_t = self.phi_x(x)
        for i, x_item in enumerate(xargs):
            if len(self.phi_x_i) < i + 1:
                # add a new model at runtime if needed
                self.phi_x_i.append(self._lazy_build_phi_x(x_item.size()[1:]))
                print("increased length of feature extractors to {}".format(len(self.phi_x_i)))

            # use the model and concat on the feature dimension
            phi_x_i = self.phi_x_i[i](x_item)
            phi_x_t = torch.cat([phi_x_t, phi_x_i], -1)

        # nan_check_and_break(phi_x_t, "phi_x_t")
        return phi_x_t

    def _lazy_build_encoder(self, input_size):
        """ lazy build the encoder based on the input size

        :param input_size: the input tensor size
        :returns: the encoder
        :rtype: nn.Module

        """
        if not hasattr(self, 'encoder'):
            self.encoder = self._get_dense_net_map('vrnn_enc')(
                input_size, self.reparameterizer.input_size,
                activation_fn=self.activation_fn,
                normalization_str=self.config['dense_normalization'],
                # activation_fn=Identity,
                # normalization_str='batchnorm',
                num_layers=2
            )

        return self.encoder

    def encode(self, x, *xargs):
        """ single sample encode using x

        :param x: the input tensor
        :returns: dict of encoded logits
        :rtype: dict

        """
        if self.config['decoder_layer_type'] == 'pixelcnn':
            x = (x - .5) * 2.

        # get the memory trace, TODO: evaluate different recovery methods below
        batch_size = x.size(0)
        final_state = torch.mean(self.memory.get_state()[0], 0)
        nan_check_and_break(final_state, "final_rnn_output")

        # extract input data features
        phi_x_t = self._extract_features(x, *xargs)

        # encoder projection
        enc_input_t = torch.cat([phi_x_t, final_state], dim=-1)
        enc_t = self._lazy_build_encoder(enc_input_t.size(-1))(enc_input_t)
        nan_check_and_break(enc_t, "enc_t")

        # prior projection , consider: + eps_fn(self.config['cuda']))
        prior_t = self.prior(final_state.contiguous())
        nan_check_and_break(prior_t, "priot_t")

        return {
            'encoder_logits': enc_t,
            'prior_logits': prior_t,
            'x_features': phi_x_t
        }

    def _decode_pixelcnn_or_normal(self, dec_input_t):
        """ helper to decode using the pixel-cnn or normal decoder

        :param dec_input_t: input decoded tensor (unactivated)
        :returns: activated tensor
        :rtype: torch.Tensor

        """
        if self.config['decoder_layer_type'] == "pixelcnn":
            # hot-swap the non-pixel CNN for the decoder
            full_decoder = self.decoder
            trunc_decoder = self.decoder[0:-1]

            # decode the synthetic samples using non-pCNN
            decoded = trunc_decoder(dec_input_t)

            # then decode with the pCNN
            return self.generate_pixel_cnn(dec_input_t.size(0), decoded)

        dec_logits_t = self.decoder(dec_input_t)
        return self.nll_activation(dec_logits_t)

    def generate_synthetic_samples(self, batch_size, **kwargs):
        """ generate batch_size samples.

        :param batch_size: the size of the batch to generate
        :returns: generated tensor
        :rtype: torch.Tensor

        """
        if 'reset_state' in kwargs and kwargs['reset_state']:
            self.memory.init_state(batch_size, cuda=self.config['cuda'])# ,
                                   # override_noisy_state=True)

        # grab the final state
        final_state = torch.mean(self.memory.get_state()[0], 0)

        # reparameterize the prior distribution
        # prior_t = self.prior(final_state.contiguous())
        # prior_t = self._clamp_variance(prior_t)
        # z_prior_t, params_prior_t = self.reparameterizer(prior_t)

        # old working-ish
        # z_prior_t = self.reparameterizer.prior(batch_size)

        if 'use_aggregate_posterior' in kwargs and kwargs['use_aggregate_posterior']:
            training_tmp = self.reparameterizer.training # XXX: over-ride training to get some stochasticity
            self.reparameterizer.train(False)
            z_prior_t, _ = self.reparameterizer(self.aggregate_posterior['prior_logits'].ema_val)
            self.reparameterizer.train(training_tmp)
        else:
            z_prior_t = self.reparameterizer.prior(
                batch_size, scale_var=self.config['generative_scale_var'], **kwargs
            )

        # encode prior sample, this contrasts the decoder where
        # the features are run through this network
        phi_z_t = self.phi_z(z_prior_t)

        # construct decoder inputs and process
        dec_input_t = torch.cat([phi_z_t, final_state], -1)
        dec_output_t = self._decode_pixelcnn_or_normal(dec_input_t)

        # decoded_list, _ = self(dec_output_t)
        # return torch.cat(decoded_list, 0)

        decoded_list = [dec_output_t]
        for _ in range(self.config['max_time_steps'] - 1):
            dec_output_tp1, _ = self.step(dec_output_t)
            dec_output_t = dec_output_t + dec_output_tp1
            decoded_list.append(dec_output_t.clone())

        #return torch.mean(torch.cat([d.unsqueeze(0) for d in decoded_list], 0), 0)
        # TODO: factor generations for multi-input
        return torch.cat(decoded_list, 0)


    def posterior(self, *x_args):
        """ encode the set of input tensor args

        :returns: reparam dict
        :rtype: dict

        """
        logits_map = self.encode(*x_args)
        if self.training:
            self.aggregate_posterior['encoder_logits'](logits_map['encoder_logits'])
            self.aggregate_posterior['prior_logits'](logits_map['prior_logits'])

        return self.reparameterize(logits_map)

    def _ensure_same_size(self, prediction_list, target_list):
        """ helper to ensure that image sizes in both lists match

        :param prediction_list: the list of predictions
        :param target_list:  the list of targers
        :returns: None
        :rtype: None

        """
        assert len(prediction_list) == len(target_list), "#preds[{}] != #targets[{}]".format(
            len(prediction_list), len(target_list))
        for i in range(len(target_list)):
            if prediction_list[i].size() != target_list[i].size():
                if prediction_list[i].size() > target_list[i].size():
                    larger_size = prediction_list[i].size()
                    target_list[i] = F.upsample(target_list[i],
                                                size=tuple(larger_size[2:]),
                                                mode='bilinear')

                else:
                    larger_size = target_list[i].size()
                    prediction_list[i] = F.upsample(prediction_list[i],
                                                    size=tuple(larger_size[2:]),
                                                    mode='bilinear')

        return prediction_list, target_list

    def kld(self, dist):
        """ KL divergence between dist_a and prior as well as constrain prior to hyper-prior

        :param dist: the distribution map
        :returns: kl divergence
        :rtype: torch.Tensor

        """
        prior_kl = self.reparameterizer.kl(dist['prior'])  \
            if self.config['use_prior_kl'] is True else 0
        return self.reparameterizer.kl(dist['posterior'], dist['prior']) + prior_kl

    def _compute_mi_params(self, recon_x_logits, params):
        """ Internal helper to compute the MI params and append to full params

        :param recon_x: reconstruction
        :param params: the original params
        :returns: original params OR param + MI_params
        :rtype: dict

        """
        if self.config['continuous_mut_info'] > 0 or self.config['discrete_mut_info'] > 0:
            _, q_z_given_xhat_params = self.posterior(self.nll_activation(recon_x_logits))
            params['posterior']['q_z_given_xhat'] = q_z_given_xhat_params['posterior']

        # base case, no MI
        return params

    def mut_info(self, dist_params, batch_size):
        """ Returns mutual information between z <-> x

        :param dist_params: the distribution dict
        :returns: tensor of dimension batch_size
        :rtype: torch.Tensor

        """
        mut_info = float_type(self.config['cuda'])(batch_size).zero_()

        # only grab the mut-info if the scalars above are set
        if (self.config['continuous_mut_info'] > 0
             or self.config['discrete_mut_info'] > 0):
            mut_info = self._clamp_mut_info(self.reparameterizer.mutual_info(dist_params['posterior']))

        return mut_info

    # def _compute_mi_params(self, recon_x_logits, params_list):
    #     """ Internal helper to compute the MI params and append to full params

    #     :param recon_x: reconstruction
    #     :param params: the original params
    #     :returns: original params OR param + MI_params
    #     :rtype: dict

    #     """
    #     if self.config['continuous_mut_info'] > 0 or self.config['discrete_mut_info'] > 0:
    #         _, q_z_given_xhat_params_list = self.posterior(self.nll_activation(recon_x_logits))
    #         for param, q_z_given_xhat in zip(params_list, q_z_given_xhat_params_list):
    #             param['q_z_given_xhat'] = q_z_given_xhat

    #         return params_list

    #     # base case, no MI
    #     return params_list

    @staticmethod
    def _add_loss_map(loss_t, loss_aggregate_map):
        """ helper to add two maps and keep counts
            of the total samples for reduction later

        :param loss_t: the loss dict
        :param loss_aggregate_map: the aggregator dict
        :returns: aggregate dict
        :rtype: dict

        """
        if loss_aggregate_map is None:
            return {**loss_t, 'count': 1}

        for (k, v) in loss_t.items():
            loss_aggregate_map[k] += v

        # increment total count
        loss_aggregate_map['count'] += 1
        return loss_aggregate_map

    @staticmethod
    def _mean_map(loss_aggregate_map):
        """ helper to reduce all values by the key count

        :param loss_aggregate_map: the aggregate dict
        :returns: count reduced dict
        :rtype: dict

        """
        for k in loss_aggregate_map.keys():
            if k == 'count':
                continue

            loss_aggregate_map[k] /= loss_aggregate_map['count']

        return loss_aggregate_map

    def loss_function(self, recon_x_container, x_container, params_map):
        """ evaluates the loss of the model by simply summing individual losses

        :param recon_x_container: the reconstruction container
        :param x_container: the input container
        :param params_map: the params dict
        :returns: the mean-reduced aggregate dict
        :rtype: dict

        """
        assert len(recon_x_container) == len(params_map)

        # case where only 1 data sample, but many posteriors
        if not isinstance(x_container, list) and len(x_container) != len(recon_x_container):
            scale = 1.0 / len(recon_x_container)
            x_container = [scale * x_container.clone() for _ in range(len(recon_x_container))]
            recon_x_container = [scale * recon_x_container[-1].clone() for _ in range(len(recon_x_container))]

        # aggregate the loss many and return the mean of the map
        loss_aggregate_map = None
        for recon_x, x, params in zip(recon_x_container, x_container, params_map):
            loss_t = super(VRNN, self).loss_function(recon_x, x, params)
            loss_aggregate_map = self._add_loss_map(loss_t, loss_aggregate_map)

        return self._mean_map(loss_aggregate_map)

    def get_activated_reconstructions(self, reconstr_container):
        """ Returns activated reconstruction

        :param reconstr: unactivated reconstr logits list
        :returns: activated reconstr
        :rtype: dict

        """
        recon_dict = {}
        for i, recon in enumerate(reconstr_container):
            recon_dict['reconstruction{}_imgs'.format(i)] = self.nll_activation(recon)

        return recon_dict

from __future__ import print_function
import pprint
import functools
import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Variable
from collections import OrderedDict, Counter

from helpers.pixel_cnn.model import PixelCNN
from helpers.utils import float_type, zeros, nan_check_and_break, one_hot_np
from helpers.layers import View, flatten_layers, Identity, EMA, \
    build_pixelcnn_decoder, add_normalization, str_to_activ_module, get_decoder, get_encoder
from helpers.distributions import nll_activation as nll_activation_fn
from helpers.distributions import nll as nll_fn
from helpers.distributions import nll_has_variance


class VarianceProjector(nn.Module):
    def __init__(self, output_shape, activation_fn, config):
        """ Simple helper to project to 2 * chans if we have variance.
            Else do nothing otherwise

        :param output_shape: the output shape to project to
        :param activation_fn: the activation to use
        :param config: argparse dict
        :returns: object
        :rtype: object

        """
        super(VarianceProjector, self).__init__()
        chans = output_shape[0]
        self.config = config

        # build the sequential layer
        if nll_has_variance(config['nll_type']):
            if config['decoder_layer_type'] in ['conv', 'coordconv', 'resnet']:
                self.decoder_projector = nn.Sequential(
                    activation_fn(),
                    nn.ConvTranspose2d(chans, chans*2, 1, stride=1, bias=False)
                )
            else: # dense projector
                input_flat = int(np.prod(output_shape))
                self.decoder_projector = nn.Sequential(
                    View([-1, input_flat]),
                    add_normalization(Identity(), config['dense_normalization'], 1, input_flat),
                    activation_fn(),
                    nn.Linear(input_flat, input_flat*2, bias=True),
                    View([-1, chans*2, *output_shape[1:]])
                )

    def forward(self, x):
        if hasattr(self, 'decoder_projector'):
            return self.decoder_projector(x)

        return x


class AbstractVAE(nn.Module):
    def __init__(self, input_shape, **kwargs):
        """ Abstract base class for VAE.

        :param input_shape: the input tensor shape
        :returns: instantiation of object
        :rtype: object

        """
        super(AbstractVAE, self).__init__()
        self.input_shape = input_shape
        self.is_color = input_shape[0] > 1
        self.chans = 3 if self.is_color else 1
        self.config = kwargs['kwargs']

        # keep track of ammortized posterior
        self.aggregate_posterior = EMA(0.999)

        # grab the activation nn.Module from the string
        self.activation_fn = str_to_activ_module(self.config['activation'])

    def get_reparameterizer_scalars(self):
        """ return the reparameterization scalars (eg: tau in gumbel)

        :returns: a dict of scalars
        :rtype: dict

        """
        return self.reparameterizer.get_reparameterizer_scalars()

    def build_encoder(self):
        """ helper to build the encoder type

        :returns: an encoder
        :rtype: nn.Module

        """
        conv_layer_types = ['conv', 'coordconv', 'resnet']
        input_shape = [self.input_shape[0], 0, 0] if self.config['encoder_layer_type'] \
            in conv_layer_types else self.input_shape

        # return the encoder
        return get_encoder(self.config)(input_shape=input_shape,
                                        output_size=self.reparameterizer.input_size,
                                        activation_fn=self.activation_fn)

    def lazy_build_decoder(self, input_size):
        """ Lazily build the decoder network given the input_size

        :param input_size: the input size of the latent vector
        :returns: a decoder
        :rtype: nn.Module

        """
        if not hasattr(self, 'decoder'):
            setattr(self, 'decoder', self._build_decoder(input_size))

        return self.decoder

    def build_decoder(self, reupsample=True):
        """ helper function to build convolutional or dense decoder

        :returns: a decoder
        :rtype: nn.Module

        """
        if self.config['decoder_layer_type'] == "pixelcnn":
            assert self.config['nll_type'] == "disc_mix_logistic", \
                "pixelcnn only works with disc_mix_logistic"

        decoder = get_decoder(self.config, reupsample)(input_size=self.reparameterizer.output_size,
                                                       output_shape=self.input_shape,
                                                       activation_fn=self.activation_fn)
        # append the variance as necessary
        return self._append_variance_projection(decoder)

    def _append_variance_projection(self, decoder):
        """ Appends a decoder variance for gaussian, etc.

        :param decoder: the nn.Module
        :returns: appended variance projector to decoder
        :rtype: nn.Module

        """

        if self.config['decoder_layer_type'] == "pixelcnn":
            # pixel CNN already accounts for variance internally
            self.pixel_cnn = PixelCNN(input_channels=self.chans,
                                      nr_resnet=2, nr_filters=40,
                                      nr_logistic_mix=10)
            decoder = nn.Sequential(
                decoder,
                #self.activation_fn(),
                #nn.Tanh(),
                nn.Sigmoid(),
                self.pixel_cnn
            )
        elif nll_has_variance(self.config['nll_type']):
            # add the variance projector (if we are in that case for the NLL)
            print("adding variance projector for {} log-likelihood".format(self.config['nll_type']))
            decoder = nn.Sequential(
                decoder,
                # TODO: if you need variance on the decoded distribution use the following:
                # VarianceProjector(self.input_shape, self.activation_fn, self.config)
            )

        return decoder

    def fp16(self):
        """ FP16-ify the model

        :returns: None
        :rtype: None

        """
        self.encoder = self.encoder.half()
        if self.config['decoder_layer_type'] == "pixelcnn":
            self.decoder = nn.Sequential(
                self.decoder[0:-1].half(),
                self.decoder[-1].half()
            )
        else:
            self.decoder = self.decoder.half()

    def parallel(self):
        """ DataParallel this module

        :returns: None
        :rtype: None

        """
        self.encoder = nn.DataParallel(self.encoder)
        if self.config['decoder_layer_type'] == "pixelcnn":
            self.decoder = nn.Sequential(
                nn.DataParallel(self.decoder[0:-1]),
                nn.DataParallel(self.decoder[-1])
            )
        else:
            self.decoder = nn.DataParallel(self.decoder)

    def compile_full_model(self):
        """ Takes all the submodules and module-lists
            and returns one gigantic sequential_model

        :returns: None
        :rtype: None

        """
        full_model_list, _ = flatten_layers(self)
        return nn.Sequential(OrderedDict(full_model_list))

    def generate_pixel_cnn(self, batch_size, decoded=None):
        """ Generates auto-regressively.

        :param batch_size: batch size for generations
        :param decoded: the input logits
        :returns: logits tensor
        :rtype: torch.Tensor

        """
        self.pixel_cnn.eval()
        with torch.no_grad():
            if decoded is None:  # use zeros if no values provided
                decoded = zeros(shape=[batch_size] + self.input_shape,
                                cuda=self.config['cuda'])

            for i in range(decoded.size(2)):         # y-axis
                for j in range(decoded.size(3)):     # x-axis
                    for c in range(decoded.size(1)): # chans
                        logits = self.pixel_cnn(decoded, sample=True)
                        out_sample = self.nll_activation(logits)
                        decoded[:, c, i, j].data = out_sample[:, c, i, j].data
                        if self.config['cuda']:
                            torch.cuda.synchronize()

        rescaling_inv = lambda x : (0.5 * x) + .5
        return rescaling_inv(decoded)
        # return decoded

    def generate_synthetic_samples(self, batch_size, **kwargs):
        """ Generates samples with VAE.

        :param batch_size: the number of samples to generate.
        :returns: decoded logits
        :rtype: torch.Tensor

        """
        if 'use_aggregate_posterior' in kwargs and kwargs['use_aggregate_posterior']:
            training_tmp = self.reparameterizer.training
            self.reparameterizer.train(False)
            z_samples, _ = self.reparameterize(self.aggregate_posterior.ema_val)
            self.reparameterizer.train(training_tmp)
        else:
            z_samples = self.reparameterizer.prior(
                    batch_size, scale_var=self.config['generative_scale_var'], **kwargs
            )

        if self.config['decoder_layer_type'] == "pixelcnn":
            # hot-swap the non-pixel CNN for the decoder
            full_decoder = self.decoder
            trunc_decoder = self.decoder[0:-1]
            self.decoder = trunc_decoder

            # decode the synthetic samples
            decoded = self.decode(z_samples)

            # swap back the decoder and run the pixelcnn
            self.decoder = full_decoder
            return self.generate_pixel_cnn(batch_size, decoded)

        # in the normal case just decode and activate
        return self.nll_activation(self.decode(z_samples))

    def generate_synthetic_sequential_samples(self, num_original_discrete, num_rows=8):
        """ Iterates over all discrete positions and generates samples (for mix or disc only).

        :param num_original_discrete: The original discrete size (useful for LLVAE).
        :param num_rows: for visdom
        :returns: decoded logits
        :rtype: torch.Tensor

        """
        assert self.has_discrete()

        # create a grid of one-hot vectors for displaying in visdom
        # uses one row for original dimension of discrete component
        discrete_indices = np.array([np.random.randint(begin, end, size=num_rows) for begin, end in
                                     zip(range(0, self.reparameterizer.config['discrete_size'],
                                               num_original_discrete),
                                         range(num_original_discrete,
                                               self.reparameterizer.config['discrete_size'] + 1,
                                               num_original_discrete))])
        discrete_indices = discrete_indices.reshape(-1)

        self.eval() # lock BN / Dropout, etc
        with torch.no_grad():
            z_samples = Variable(torch.from_numpy(
                one_hot_np(self.reparameterizer.config['discrete_size'],
                           discrete_indices))
            )
            z_samples = z_samples.type(float_type(self.config['cuda']))

            if self.config['reparam_type'] == 'mixture' and self.config['vae_type'] != 'sequential':
                ''' add in the gaussian prior '''
                z_cont = self.reparameterizer.continuous.prior(z_samples.size(0))
                z_samples = torch.cat([z_cont, z_samples], dim=-1)

            # the below is to handle the issues with BN
            # pad the z to be full batch size
            number_to_return = z_samples.shape[0] # original generate number
            number_batches_z = int(max(1, np.ceil(
                float(self.config['batch_size']) / float(number_to_return))))
            z_padded = torch.cat(
                [z_samples for _ in range(number_batches_z)], 0
            )[0:self.config['batch_size']]

            # generate and return the requested number
            number_batches_to_generate = int(max(1, np.ceil(
                float(number_to_return) / float(self.config['batch_size']))))
            generated = torch.cat([self.generate_synthetic_samples(
                self.config['batch_size'], z_samples=z_padded
            ) for _ in range(number_batches_to_generate)], 0)
            return generated[0:number_to_return] # only return num_requested

    def nll_activation(self, logits):
        """ Activates the logits

        :param logits: the unactivated logits
        :returns: activated logits.
        :rtype: torch.Tensor

        """
        return nll_activation_fn(logits,
                                 self.config['nll_type'],
                                 chans=self.chans)

    def has_discrete(self):
        """ True is we have a discrete reparameterization

        :returns: boolean
        :rtype: bool

        """
        return self.config['reparam_type'] == 'mixture' \
            or self.config['reparam_type'] == 'discrete'


    def forward(self, x):
        """ Accepts input, gets posterior and latent and decodes.

        :param x: input tensor.
        :returns: decoded logits and reparam dict
        :rtype: torch.Tensor, dict

        """
        z, params = self.posterior(x)
        decoded_logits = self.decode(z)
        params = self._compute_mi_params(decoded_logits, params)
        return decoded_logits, params

    def loss_function(self, recon_x, x, params):
        """ Produces ELBO, handles mutual info and proxy loss terms too.

        :param recon_x: the unactivated reconstruction preds.
        :param x: input tensor.
        :param params: the dict of reparameterization.
        :param mut_info: the calculated mutual info.
        :returns: loss dict
        :rtype: dict

        """
        if self.config['decoder_layer_type'] == 'pixelcnn':
            x = (x - .5) * 2.

        nll = nll_fn(x, recon_x, self.config['nll_type'])
        nan_check_and_break(nll, "nll")
        kld = self.kld(params)
        nan_check_and_break(kld, "kld")
        elbo = nll + kld  # save the base ELBO, but use the beta-vae elbo for the full loss

        # add the proxy loss if it exists
        proxy_loss = self.reparameterizer.proxy_layer.loss_function() \
            if hasattr(self.reparameterizer, 'proxy_layer') else torch.zeros_like(elbo)

        # handle the mutual information term
        mut_info = self.mut_info(params, x.size(0))

        loss = (nll + self.config['kl_beta'] * kld) - mut_info
        return {
            'loss': loss,
            'loss_mean': torch.mean(loss),
            'elbo_mean': torch.mean(elbo),
            'nll_mean': torch.mean(nll),
            'kld_mean': torch.mean(kld),
            'proxy_mean': torch.mean(proxy_loss),
            'mut_info_mean': torch.mean(mut_info)
        }

    def has_discrete(self):
        """ returns True if the model has a discrete
            as it's first (in the case of parallel) reparameterizer

        :returns: True/False
        :rtype: bool

        """
        raise NotImplementedError("has_discrete not implemented")

    def get_reparameterizer_scalars(self):
        """ returns a map of the scalars of the reparameterizers.
            This is useful for visualization purposes

        :returns: dict of scalars
        :rtype: dict

        """
        return self.reparameterizer.get_reparameterizer_scalars()

    def reparameterize(self, logits):
        """ Reparameterize the logits and returns a dict.

        :param logits: unactivated encoded logits.
        :returns: reparam dict
        :rtype: dict

        """
        return self.reparameterizer(logits)

    def decode(self, z):
        """ Decode a latent z back to x.

        :param z: the latent tensor.
        :returns: decoded logits (unactivated).
        :rtype: torch.Tensor

        """
        return self.decoder(z.contiguous())

    def posterior(self, x):
        """ get a reparameterized Q(z|x) for a given x

        :param x: input tensor
        :returns: reparam dict
        :rtype: torch.Tensor

        """
        z_logits = self.encode(x)               # encode logits
        if self.training:
            self.aggregate_posterior(z_logits)  # aggregate posterior

        return self.reparameterize(z_logits)    # return reparameterized value

    def encode(self, x):
        """ Encodes a tensor x to a set of logits.

        :param x: the input tensor
        :returns: logits
        :rtype: torch.Tensor

        """
        if self.config['decoder_layer_type'] == 'pixelcnn':
            x = (x - .5) * 2.

        # print('[ORIG] x max = ', x.max(), " min = ", x.min())
        return self.encoder(x)

    def kld(self, dist_a):
        """ KL-Divergence of the distribution dict and the prior of that distribution.

        :param dist_a: the distribution dict.
        :returns: tensor that is of dimension batch_size
        :rtype: torch.Tensor

        """
        return self.reparameterizer.kl(dist_a)

    def _clamp_mut_info(self, mut_info):
        """ helper to clamp the mutual information according to a predefined strategy

        :param mut_info: the tensor of mut-info
        :returns: clamped mut-info
        :rtype: torch.Tensor

        """
        mut_clamp_strategy_map = {                # Clamping strategies
            'none': lambda mut_info: mut_info,
            'norm': lambda mut_info: mut_info / torch.norm(mut_info, p=2),
            'clamp': lambda mut_info: torch.clamp(mut_info,
                                                  min=-self.config['mut_clamp_value'],
                                                  max=self.config['mut_clamp_value'])
        }
        return mut_clamp_strategy_map[self.config['mut_clamp_strategy'].strip().lower()](mut_info)

    def _compute_mi_params(self, recon_x_logits, params):
        """ Internal helper to compute the MI params and append to full params

        :param recon_x: reconstruction
        :param params: the original params
        :returns: original params OR param + MI_params
        :rtype: dict

        """
        if self.config['continuous_mut_info'] > 0 or self.config['discrete_mut_info'] > 0:
            _, q_z_given_xhat_params = self.posterior(self.nll_activation(recon_x_logits))
            return {**params, 'q_z_given_xhat': q_z_given_xhat_params}

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
            mut_info = self._clamp_mut_info(self.reparameterizer.mutual_info(dist_params))

        return mut_info

    def get_activated_reconstructions(self, reconstr):
        """ Returns activated reconstruction

        :param reconstr: unactivated reconstr logits
        :returns: activated reconstr
        :rtype: torch.Tensor

        """
        return {'reconstruction_imgs': self.nll_activation(reconstr)}

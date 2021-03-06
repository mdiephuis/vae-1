from __future__ import print_function
import pprint
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.distributions as D
from torch.autograd import Variable

from helpers.utils import float_type, one_hot, ones_like, long_type


class GumbelSoftmax(nn.Module):
    def __init__(self, config, dim=-1):
        """ Gumbel Softmax reparameterization of Categorical distribution.

        :param config: argparse
        :param dim: dim to operate over
        :returns: GumberlSoftmax object
        :rtype: nn.Module

        """
        super(GumbelSoftmax, self).__init__()
        self._setup_anneal_params()
        self.dim = dim
        self.iteration = 0
        self.config = config
        self.input_size = self.config['discrete_size']
        self.output_size = self.config['discrete_size']

    def prior(self, batch_size, **kwargs):
        """ Sample the prior for batch_size samples.

        :param batch_size: number of prior samples.
        :returns: prior
        :rtype: torch.Tensor

        """
        uniform_probs = float_type(self.config['cuda'])(1, self.output_size).zero_()
        uniform_probs += 1.0 / self.output_size
        cat = torch.distributions.Categorical(uniform_probs)
        sample = cat.sample((batch_size,))
        return Variable(
            one_hot(self.output_size, sample, use_cuda=self.config['cuda'])
        ).type(float_type(self.config['cuda']))

    def get_reparameterizer_scalars(self):
        """ Returns any scalars used in reparameterization.

        :returns: dict of scalars
        :rtype: dict

        """
        return {'tau_scalar': self.tau}

    def _setup_anneal_params(self):
        """ Setup the base gumbel rates.
            TODO: needs parameterization in argparse.

        :returns: None
        :rtype: None

        """
        self.tau, self.tau0 = 1.0, 1.0
        self.anneal_rate = 3e-6
        self.min_temp = 0.5

    def anneal(self, anneal_interval=10):
        """ Helper to anneal the temperature.

        :param anneal_interval: the interval to employ annealing.
        :returns: None
        :rtype: None

        """
        if self.training \
           and self.iteration > 0 \
           and self.iteration % anneal_interval == 0:

            # smoother annealing
            rate = -self.anneal_rate * self.iteration
            self.tau = np.maximum(self.tau0 * np.exp(rate),
                                  self.min_temp)
            # hard annealing
            # self.tau = np.maximum(0.9 * self.tau, self.min_temp)

    def reparmeterize(self, logits):
        """ Given logits reparameterize to a categorical

        :param logits: unactivated logits
        :returns: reparameterized tensor (if training), hard version, soft version.
        :rtype: torch.Tensor, torch.Tensor, torch.Tensor

        """
        logits_shp = logits.size()
        log_q_z = F.log_softmax(logits, dim=self.dim)
        z, z_hard = self.sample_gumbel(logits, self.tau,
                                       hard=True,
                                       dim=self.dim,
                                       use_cuda=logits.is_cuda)
        return z.view(logits_shp), z_hard.view(logits_shp), log_q_z

    def mutual_info_analytic(self, params, eps=1e-9):
        """ I(z_d; x) ~ H(z_prior, z_d) + H(z_prior), i.e. analytic version.

        :param params: parameters of distribution
        :param eps: tolerance
        :returns: batch_size mutual information (prop-to) tensor.
        :rtype: torch.Tensor

        """
        targets = torch.argmax(params['discrete']['z_hard'].type(long_type(self.config['cuda'])), dim=-1)
        crossent_loss = -F.cross_entropy(input=params['q_z_given_xhat']['discrete']['logits'],
                                         target=targets, reduce=False)
        ent_loss = -torch.sum(D.OneHotCategorical(logits=params['discrete']['z_hard']).entropy(), -1)
        return ent_loss + crossent_loss

    def mutual_info_monte_carlo(self, params, eps=1e-9):
        """ I(z_d; x) ~ H(z_prior, z_d) + H(z_prior)
            but does the single-sample monte-carlo approximation of it.

        :param params: parameters of distribution
        :param eps: tolerance
        :returns: batch_size mutual information (prop-to) tensor.
        :rtype: torch.Tensor

        """
        log_q_z_given_x = params['q_z_given_xhat']['discrete']['logits'] + eps
        # log_q_z_given_x = params['discrete']['log_q_z'] + eps
        p_z = self.prior(log_q_z_given_x.size()[0])
        # p_z = params['discrete']['z_soft'] + eps
        crossent_loss = -torch.sum(log_q_z_given_x * p_z, dim=-1)
        ent_loss = -torch.sum(torch.log(p_z + eps) * p_z, dim=-1)
        return ent_loss + crossent_loss

    def mutual_info(self, params, eps=1e-9):
        """ Returns Ent + xent where xent is taken against hard targets.

        :param params: distribution parameters
        :param eps: tolerance
        :returns: batch_size tensor of mutual info
        :rtype: torch.Tensor

        """
        targets = torch.argmax(params['discrete']['z_hard'].type(long_type(self.config['cuda'])), dim=-1)
        # soft_targets = F.softmax(
        #     params['discrete']['logits'], -1
        # ).type(long_type(self.config['cuda']))
        # targets = torch.argmax(params['discrete']['log_q_z'], -1) # 3rd change, havent tried
        crossent_loss = -F.cross_entropy(input=params['q_z_given_xhat']['discrete']['logits'],
                                         target=targets, reduce=False)
        ent_loss = -torch.sum(D.OneHotCategorical(logits=params['discrete']['z_hard']).entropy(), -1)
        return self.config['discrete_mut_info'] * (ent_loss + crossent_loss)

    @staticmethod
    def _kld_categorical_uniform(log_q_z, dim=-1, eps=1e-9):
        """ KL divergence against a uniform categorical prior

        :param log_q_z: the soft logits
        :param dim: which dim to operate over
        :param eps: tolerance
        :returns: tensor of batch_size
        :rtype: torch.Tensor

        """
        shp = log_q_z.size()
        p_z = 1.0 / shp[dim]
        log_p_z = np.log(p_z)
        kld_element = log_q_z.exp() * (log_q_z - log_p_z)
        return kld_element

    def kl(self, dist_a, prior=None):
        """ KL divergence of dist_a against a prior, if none then Cat(1/k)

        :param dist_a: the distribution parameters
        :param prior: prior parameters (or None)
        :returns: batch_size kl-div tensor
        :rtype: torch.Tensor

        """
        if prior == None:  # use standard uniform prior
            return torch.sum(GumbelSoftmax._kld_categorical_uniform(
                dist_a['discrete']['log_q_z'], dim=self.dim
            ), -1)

        # we have two distributions provided (eg: VRNN)
        return D.kl_divergence(
            D.OneHotCategorical(logits=dist_a['discrete']['log_q_z']),
            D.OneHotCategorical(prior['discrete']['log_q_z'])
        )


    @staticmethod
    def _gumbel_softmax(x, tau, eps=1e-9, dim=-1, use_cuda=False):
        """ Internal gumbel softmax call using temp tau: -ln(-ln(U + eps) + eps)

        :param x: input tensor
        :param tau: temperature
        :param eps: toleranace
        :param dim: dimension to operate over
        :param use_cuda: whether or not to use cuda
        :returns: gumbel annealed tensor
        :rtype: torch.Tensor

        """
        noise = torch.rand(x.size())
        noise.add_(eps).log_().neg_()
        noise.add_(eps).log_().neg_()
        if use_cuda:
            noise = noise.cuda()

        noise = Variable(noise)
        x = (x + noise) / tau
        x = F.softmax(x + eps, dim=dim)
        return x.view_as(x)

    @staticmethod
    def sample_gumbel(x, tau, hard=False, dim=-1, use_cuda=False):
        """ Sample from the gumbel distribution and return hard and soft versions.

        :param x: the input tensor
        :param tau: temperature
        :param hard: whether to generate hard version (argmax)
        :param dim: dimension to operate over
        :param use_cuda: whether or not to use cuda
        :returns: soft, hard or soft, None
        :rtype: torch.Tensor, Optional(torch.Tensor, None)

        """
        y = GumbelSoftmax._gumbel_softmax(x, tau, dim=dim, use_cuda=use_cuda)

        if hard:
            y_max, _ = torch.max(y, dim=dim, keepdim=True)
            y_hard = Variable(
                torch.eq(y_max.data, y.data).type(float_type(use_cuda))
            )
            y_hard_diff = y_hard - y
            y_hard = y_hard_diff.detach() + y
            return y.view_as(x), y_hard.view_as(x)

        return y.view_as(x), None

    def log_likelihood(self, z, params):
        """ Log-likelihood of z induced under params.

        :param z: inferred latent z
        :param params: the params of the distribution
        :returns: log-likelihood
        :rtype: torch.Tensor

        """
        return D.Categorical(logits=params['discrete']['logits']).log_prob(z)

    def forward(self, logits):
        """ Returns a reparameterized categorical and it's params.

        :param logits: unactivated logits.
        :returns: reparam tensor and params.
        :rtype: torch.Tensor, dict

        """
        self.anneal()  # anneal first
        z, z_hard, log_q_z = self.reparmeterize(logits)
        params = {
            'z_hard': z_hard,
            'logits': logits,
            'log_q_z': log_q_z,
            'tau_scalar': self.tau
        }
        self.iteration += 1

        if self.training:
            # return the reparameterization
            # and the params of gumbel
            return z, { 'z': z, 'discrete': params }

        return z_hard, { 'z': z, 'discrete': params }

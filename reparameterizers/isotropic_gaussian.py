from __future__ import print_function
import numpy as np
import torch
import torch.nn as nn
import torch.distributions as D
import torch.nn.functional as F
from torch.autograd import Variable

from helpers.utils import zeros_like, ones_like, same_type, \
    float_type, nan_check_and_break, is_half
from helpers.utils import eps as eps_fn


class IsotropicGaussian(nn.Module):
    def __init__(self, config):
        """ typical isotropic gaussian reparameterization.

        :param config: argparse
        :returns: Isotropicgaussian module
        :rtype: nn.Module

        """
        super(IsotropicGaussian, self).__init__()
        self.config = config
        self.input_size = self.config['continuous_size']
        assert self.config['continuous_size'] % 2 == 0
        self.output_size = self.config['continuous_size'] // 2

    def prior(self, batch_size, **kwargs):
        """ Sample the prior for batch_size samples.

        :param batch_size: number of prior samples.
        :returns: prior
        :rtype: torch.Tensor

        """
        scale_var = 1.0 if 'scale_var' not in kwargs else kwargs['scale_var']
        return Variable(
            same_type(self.config['half'], self.config['cuda'])(
                batch_size, self.output_size
            ).normal_(mean=0, std=scale_var)
        )

    def _reparametrize_gaussian(self, mu, logvar):
        """ Internal member to reparametrize gaussian.

        :param mu: mean logits
        :param logvar: log-variance.
        :returns: reparameterized tensor and param dict
        :rtype: torch.Tensor, dict

        """
        if self.training: # returns a stochastic sample for training
            std = logvar.mul(0.5).exp()
            eps = same_type(is_half(logvar), logvar.is_cuda)(
                logvar.size()
            ).normal_()
            eps = Variable(eps)
            nan_check_and_break(logvar, "logvar")
            return eps.mul(std).add_(mu), {'mu': mu, 'logvar': logvar}

        return mu, {'mu': mu, 'logvar': logvar}

    def reparmeterize(self, logits):
        """ Given logits reparameterize to a gaussian using
            first half of features for mean and second half for std.

        :param logits: unactivated logits
        :returns: reparameterized tensor (if training), param dict
        :rtype: torch.Tensor, dict

        """
        eps = eps_fn(self.config['half'])
        feature_size = logits.size(-1)
        assert feature_size % 2 == 0 and feature_size // 2 == self.output_size
        if logits.dim() == 2:
            mu = logits[:, 0:int(feature_size/2)]
            nan_check_and_break(mu, "mu")
            sigma = logits[:, int(feature_size/2):] + eps
            # sigma = F.softplus(logits[:, int(feature_size/2):]) + eps
            # sigma = F.hardtanh(logits[:, int(feature_size/2):], min_val=-6.,max_val=2.)
        elif logits.dim() == 3:
            mu = logits[:, :, 0:int(feature_size/2)]
            sigma = logits[:, :, int(feature_size/2):] + eps
        else:
            raise Exception("unknown number of dims for isotropic gauss reparam")

        return self._reparametrize_gaussian(mu, sigma)

    def get_reparameterizer_scalars(self):
        """ Returns any scalars used in reparameterization.

        :returns: dict of scalars
        :rtype: dict

        """
        return {}

    def mutual_info(self, params, eps=1e-9):
        """ I(z_d; x) ~ H(z_prior, z_d) + H(z_prior)

        :param params: parameters of distribution
        :param eps: tolerance
        :returns: batch_size mutual information (prop-to) tensor.
        :rtype: torch.Tensor

        """
        z_true = D.Normal(params['gaussian']['mu'],
                          params['gaussian']['logvar'])
        z_match = D.Normal(params['q_z_given_xhat']['gaussian']['mu'],
                           params['q_z_given_xhat']['gaussian']['logvar'])
        kl_proxy_to_xent = torch.sum(D.kl_divergence(z_match, z_true), dim=-1)
        return self.config['continuous_mut_info'] * kl_proxy_to_xent

    @staticmethod
    def _kld_gaussian_N_0_1(mu, logvar):
        """ Internal member for kl-div against a N(0, 1) prior

        :param mu: mean
        :param logvar: log-variance
        :returns: batch_size tensor of kld
        :rtype: torch.Tensor

        """
        standard_normal = D.Normal(zeros_like(mu), ones_like(logvar))
        normal = D.Normal(mu, logvar)
        return torch.sum(D.kl_divergence(normal, standard_normal), -1)

    def kl(self, dist_a, prior=None):
        """ KL divergence of dist_a against a prior, if none then N(0, 1)

        :param dist_a: the distribution parameters
        :param prior: prior parameters (or None)
        :returns: batch_size kl-div tensor
        :rtype: torch.Tensor

        """
        if prior == None: # use default prior
            return IsotropicGaussian._kld_gaussian_N_0_1(
                dist_a['gaussian']['mu'], dist_a['gaussian']['logvar']
            )

        # we have two distributions provided (eg: VRNN)
        return torch.sum(D.kl_divergence(
            D.Normal(dist_a['gaussian']['mu'], dist_a['gaussian']['logvar']),
            D.Normal(prior['gaussian']['mu'], prior['gaussian']['logvar'])
        ), -1)

    def log_likelihood(self, z, params):
        """ Log-likelihood of z induced under params.

        :param z: inferred latent z
        :param params: the params of the distribution
        :returns: log-likelihood
        :rtype: torch.Tensor

        """
        return D.Normal(params['gaussian']['mu'],
                        params['gaussian']['logvar']).log_prob(z)

    def forward(self, logits):
        """ Returns a reparameterized gaussian and it's params.

        :param logits: unactivated logits.
        :returns: reparam tensor and params.
        :rtype: torch.Tensor, dict

        """
        z, gauss_params = self.reparmeterize(logits)
        gauss_params['mu_mean'] = torch.mean(gauss_params['mu'])
        gauss_params['logvar_mean'] = torch.mean(gauss_params['logvar'])
        return z, { 'z': z, 'logits': logits, 'gaussian':  gauss_params }

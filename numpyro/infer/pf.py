# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from functools import namedtuple, partial

import tqdm

import jax
from jax import jit, lax, random, grad, vmap
import jax.numpy as jnp

import numpyro
import numpyro.distributions as dist
from numpyro.distributions import constraints
from numpyro.distributions.transforms import biject_to
from numpyro.handlers import replay, seed, trace, block
from numpyro.infer.util import transform_fn

from numpyro.util import enable_x64
from numpyro.infer.util import init_to_uniform, initialize_model


PFState = namedtuple('PFState', ['particles', 'lr'])


class PF(object):
    """
    """
    def __init__(self, model, num_particles, lr=0.001, gamma=1.0, **static_kwargs):
        self.model = model
        self.num_particles = num_particles
        self.lr = lr
        self.gamma = gamma
        self.static_kwargs = static_kwargs
        self.constrain_fn = None
        self.init_loc_fn = init_to_uniform

    def init(self, *args, **kwargs):
        """
        Gets the initial PF state.

        :return: the initial :data:`PFState`
        """
        rng_key = jax.random.PRNGKey(0) # numpyro.prng_key()
        with block():
            init_params, potential_fn, self._postprocess_fn, self.prototype_trace = initialize_model(
                rng_key, self.model,
                init_strategy=self.init_loc_fn,
                dynamic_args=False,
                model_args=args,
                model_kwargs=kwargs)

        init_params, self._unravel_fn = jax.flatten_util.ravel_pytree(init_params[0])
        self._potential_fn = lambda x: potential_fn(self._unravel_fn(x))
        self.latent_dim = init_params.shape[0]
        particles = random.normal(rng_key, shape=(self.num_particles, self.latent_dim))

        return PFState(particles, self.lr)

    def get_params(self, pf_state):
        """
        Gets values at `param` sites of the `model` and `guide`.

        :param svi_state: current state of SVI.
        :return: the corresponding parameters
        """
        params = pf_state.particles
        # params = self.constrain_fn(self.optim.get_params(svi_state.optim_state))
        # return params and its postprocessed values
        unconstrained_params = vmap(self._unravel_fn)(params)
        constrained_params = vmap(self._postprocess_fn)(unconstrained_params)
        return params, unconstrained_params, constrained_params

    def update(self, pf_state, *args, **kwargs):
        """
        :param pf_state: current state of PF.
        :param args: arguments to the model / guide (these can possibly vary during
            the course of fitting).
        :param kwargs: keyword arguments to the model / guide (these can possibly vary
            during the course of fitting).
        :return: pf_state
        """
        particles, lr = pf_state
        g = vmap(lambda p: grad(self._potential_fn)(p))(particles)
        centered_particles = particles - particles.mean(0)
        quadratic_term = (centered_particles[:, None, :] @ centered_particles.T)[:, 0, :].T @ g / self.num_particles
        new_particles = particles - lr * (g.mean(0) + quadratic_term - centered_particles)
        new_lr = pf_state.lr * self.gamma
        return PFState(new_particles, new_lr)

    def run(self, num_steps):
        """
        :param int num_steps: the number of optimization steps.
        """
        def body_fn(pf_state, _):
            pf_state = self.update(pf_state)
            return pf_state, None

        pf_state = self.init()
        pf_state = lax.scan(body_fn, pf_state, None, length=num_steps)[0]

        return self.get_params(pf_state)


enable_x64()

def model(rho=0.9):
    cov = jnp.array([[10.0, rho], [rho, 0.1]])
    x = numpyro.sample("x", dist.MultivariateNormal(jnp.array([-1.0, 1.0]), covariance_matrix=cov))

num_steps = 50_000
gamma = 0.1 ** (1 / num_steps)
pf = PF(model, 3, lr=0.003, gamma=gamma)
particles = pf.run(num_steps)[0]

print("particles\n", particles)
mean = particles.mean(0)
print("mean", mean)
delta = particles - mean
cov = delta.T @ delta / particles.shape[0]
print("cov\n", cov)

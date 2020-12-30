# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial

from jax import device_put, grad, lax, ops, random, value_and_grad, tree_map
import jax.numpy as jnp

from numpyro.handlers import seed, trace
from numpyro.infer.hmc import momentum_generator
from numpyro.infer.hmc_gibbs import HMCGibbs, HMCGibbsState
from numpyro.infer.hmc_util import euclidean_kinetic_energy
from numpyro.util import (fori_loop, identity, ravel_pytree, while_loop)


def _discrete_step(rng_key, z_discrete, pe, ke_discrete, time_to_go, max_time,
                   potential_fn, z_continuous, support_sizes):
    rng_key, rng_proposal = random.split(rng_key)

    # get z proposal
    z_discrete_flat, unravel_fn = ravel_pytree(z_discrete)
    idx = jnp.argmin(time_to_go)
    proposal = random.randint(rng_proposal, (), minval=0, maxval=support_sizes[idx] - 1)
    proposal = jnp.where(proposal >= z_discrete_flat[idx], proposal + 1, proposal)
    z_discrete_new_flat = ops.index_update(z_discrete_flat, idx, proposal)
    z_discrete_new = unravel_fn(z_discrete_new_flat)

    pe_new = potential_fn(z_discrete_new, z_continuous)
    ke_discrete_i_new = ke_discrete[idx] + pe - pe_new

    z_discrete, pe, ke_discrete_i = lax.cond(ke_discrete_i_new > 0,
                                             (z_discrete_new, pe_new, ke_discrete_i_new), identity,
                                             (z_discrete, pe, ke_discrete[idx]), identity)
    ke_discrete = ops.index_update(ke_discrete, idx, ke_discrete_i)

    max_time = max_time - time_to_go[idx]
    time_to_go = time_to_go - time_to_go[idx]
    time_to_go = ops.index_update(time_to_go, idx, 1)
    return rng_key, z_discrete, pe, ke_discrete, time_to_go, max_time


def _identity_gibbs_fn(rng_key, gibbs_sites, hmc_sites):
    return gibbs_sites


class MixedHMC(HMCGibbs):

    def __init__(self, inner_kernel, discrete_sites=None, num_trajectories=1,
                 num_discrete_steps=1, discrete_mass=1.):
        super().__init__(inner_kernel, _identity_gibbs_fn, discrete_sites)
        self._num_trajectories = num_trajectories
        self._num_discrete_steps = num_discrete_steps
        self._discrete_mass = discrete_mass

    def init(self, rng_key, num_warmup, init_params, model_args, model_kwargs):
        model_kwargs = {} if model_kwargs is None else model_kwargs.copy()
        rng_key, key_u, key_z = random.split(rng_key, 3)
        prototype_trace = trace(seed(self.model, key_u)).get_trace(*model_args, **model_kwargs)
        # by default, mixed sites will include discrete sites with finite support
        # and having event_dim > 0; sites with event_dim = 0 can be added through
        # `sites` keyword in the constructor.
        discrete_sites = {name: site["value"] for name, site in prototype_trace.items()
                          if site["type"] == "sample" and site["fn"].has_enumerate_support
                          and (site["fn"].event_dim > 0 or
                               (self._discrete_sites is not None and name in self._discrete_sites))}
        # store support sizes of flatten discrete latent values
        supports = {name: jnp.broadcast_to(
                        prototype_trace[name]["fn"].enumerate_support(False).shape[0],
                        jnp.shape(value))
                    for name, value in discrete_sites.items()}
        self._support_sizes, _ = ravel_pytree(supports)
        if self._num_discrete_steps is None:
            self._num_discrete_steps = max(self._support_sizes.shape[0], self.num_trajectories + 1)
        model_kwargs["_discrete_sites"] = discrete_sites
        hmc_state = self.inner_kernel.init(key_z, num_warmup * self.num_trajectories,
                                           init_params, model_args, model_kwargs)
        z = {**discrete_sites, **hmc_state.z}
        return device_put(HMCGibbsState(z, hmc_state, rng_key))

    def sample(self, state, model_args, model_kwargs):
        # NB: this adjusts MixedHMC algorithm a bit to be more compatible to NUTS sampling
        model_kwargs = {} if model_kwargs is None else model_kwargs
        rng_key, rng_ke, rng_time = random.split(state.rng_key, 3)

        # TODO: adaptive mass for discrete particles
        ke_discrete = random.exponential(rng_ke, self._support_sizes.shape) / self._discrete_mass

        # TODO: relax the assumption that each size of the torus is proportional to velocity
        time_to_go = random.uniform(rng_time, self._support_sizes.shape)

        # run discrete steps for those time < max_time
        if self._max_times is None:
            max_time = self._num_discrete_steps / (self.num_trajectories + 1) / time_to_go.shape[0]
            max_times = [max_time, max_time]
        else:
            max_times = self._max_times

        def potential_fn(z_discrete, z_continous):
            return self.inner_kernel._potential_fn_gen(
                *model_args, _discrete_sites=z_discrete, **model_kwargs)(z_continous)

        def discrete_cond_fn(vals):
            *_, time_to_go, max_time = vals
            return jnp.amin(time_to_go) <= max_time

        def discrete_body_fn(z_continuous, vals):
            return _discrete_step(*vals,
                                  potential_fn=potential_fn,
                                  z_continuous=z_continuous,
                                  support_sizes=self._support_sizes)

        z_discrete = {k: v for k, v in state.z.items() if k not in state.hmc_state.z}
        # TODO: it is also fine to run hmc -> gibbs -> hmc so consider implementing it
        # to compare
        hmc_state = state.hmc_state

        def body_fn(i, vals):
            rng_key, z_discrete, ke_discrete, time_to_go, hmc_state = vals
            pe = hmc_state.potential_energy
            max_time = max_times[0]
            discrete_step_vals = (rng_key, z_discrete, pe, ke_discrete, time_to_go, max_time)
            rng_key, z_discrete, pe, ke_discrete, time_to_go, max_time = while_loop(
                discrete_cond_fn, partial(discrete_body_fn, hmc_state.z), discrete_step_vals)
            time_to_go = time_to_go - max_time

            # build a trajectory
            model_kwargs_ = model_kwargs.copy()
            model_kwargs_["_discrete_sites"] = z_discrete
            z_grad = grad(partial(potential_fn, z_discrete))(hmc_state.z)
            # XXX: keep kinetic energy but make direction diffuse
            if self._diffuse_momentum:
                rng_key, rng_r = random.split(rng_key)
                r = momentum_generator(hmc_state.r, hmc_state.adapt_state.mass_matrix_sqrt, rng_r)
                # scale to keep the same kinetic energy
                prev_ke = euclidean_kinetic_energy(hmc_state.adapt_state.inverse_mass_matrix, hmc_state.r)
                curr_ke = euclidean_kinetic_energy(hmc_state.adapt_state.inverse_mass_matrix, r)
                r = tree_map(lambda x: x * jnp.sqrt(prev_ke / curr_ke), r)
            else:
                r = hmc_state.r
            hmc_state = hmc_state._replace(r=r, z_grad=z_grad, potential_energy=pe)
            hmc_state = self.inner_kernel.sample(hmc_state, model_args, model_kwargs_,
                                                 reset_momentum=(i == 0))
            return rng_key, z_discrete, ke_discrete, time_to_go, hmc_state

        vals = (rng_key, z_discrete, ke_discrete, time_to_go, hmc_state)
        rng_key, z_discrete, ke_discerete, time_to_go, hmc_state = fori_loop(
            0, self.num_trajectories, body_fn, vals)

        # run discrete steps for those time < max_time
        pe = hmc_state.potential_energy
        vals = (rng_key, z_discrete, pe, ke_discrete, time_to_go, max_times[1])
        rng_key, z_discrete, pe, ke_discrete, *_ = while_loop(
            discrete_cond_fn, partial(discrete_body_fn, hmc_state.z), vals)
        hmc_state = hmc_state._replace(potential_energy=pe)

        z = {**z_discrete, **hmc_state.z}
        return HMCGibbsState(z, hmc_state, rng_key)


class DRHMCGibbs(HMCGibbs):
    def sample(self, state, model_args, model_kwargs):
        model_kwargs = {} if model_kwargs is None else model_kwargs
        rng_key, rng_gibbs = random.split(state.rng_key)

        def potential_fn(z_gibbs, z_hmc):
            return self.inner_kernel._potential_fn_gen(
                *model_args, _gibbs_sites=z_gibbs, **model_kwargs)(z_hmc)

        z_gibbs = {k: v for k, v in state.z.items() if k not in state.hmc_state.z}
        z_hmc = {k: v for k, v in state.z.items() if k in state.hmc_state.z}
        model_kwargs_ = model_kwargs.copy()
        model_kwargs_["_gibbs_sites"] = z_gibbs
        z_hmc = self.inner_kernel.postprocess_fn(model_args, model_kwargs_)(z_hmc)

        z_gibbs = self._gibbs_fn(rng_key=rng_gibbs, gibbs_sites=z_gibbs, hmc_sites=z_hmc)

        pe, z_grad = value_and_grad(partial(potential_fn, z_gibbs))(state.hmc_state.z)
        hmc_state = state.hmc_state._replace(z_grad=z_grad, potential_energy=pe)

        # TODO: compute delta energy, then join with self._gibbs_fn log ratio

        model_kwargs_["_gibbs_sites"] = z_gibbs
        hmc_state = self.inner_kernel.sample(hmc_state, model_args, model_kwargs_)

        z = {**z_gibbs, **hmc_state.z}

        return HMCGibbsState(z, hmc_state, rng_key)

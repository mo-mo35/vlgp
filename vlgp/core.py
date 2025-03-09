"""
The inference algorithm
introduce a new format of fit
trial isolation
unequal trial ready
"""
import logging

import click
import numpy as np
from numpy import identity, einsum, trace
from scipy.linalg import solve, norm, svd, LinAlgError

from . import gp
from .evaluation import timer
from .math import trunc_exp
from .util import clip

logger = logging.getLogger(__name__)


def infer_single_trial(trial, params, config):
    max_iter = config["Eniter"]
    if max_iter < 1:
        return

    zdim = params["zdim"]
    rank = params["rank"]  # rank of prior covariance
    likelihood = params["likelihood"]

    # misc
    dmu_bound = config["dmu_bound"]
    tol = config["tol"]
    method = config["method"]

    poisson_channel = likelihood == "poisson"
    gaussian_channel = likelihood == "gaussian"

    # parameters
    a = params["a"]
    b = params["b"]
    noise = params["noise"]
    gauss_noise = noise[gaussian_channel]

    Ir = identity(rank)
    # boolean indexing creates copies
    # pull indexing out of the loop for performance

    y = trial["y"]
    x = trial["x"]
    mu = trial["mu"]
    w = trial["w"]
    v = trial["v"]
    dmu = trial["dmu"]

    prior = params["cholesky"][
        y.shape[0]
    ]  # TODO: adapt unequal lengths, move into trials

    residual = np.empty_like(y, dtype=float)
    U = np.empty_like(y, dtype=float)

    y_poiss = y[:, poisson_channel]
    y_gauss = y[:, gaussian_channel]

    xb = einsum("ijk, jk -> ik", x, b)

    for i in range(max_iter):
        eta = mu @ a + xb
        r = trunc_exp(eta + 0.5 * v @ (a ** 2))

        # mean of y
        mean_gauss = eta[:, gaussian_channel]
        mean_poiss = r[:, poisson_channel]

        for l in range(zdim):
            G = prior[l]

            # working residuals
            # extensible to many other distributions
            # see GLM's working residuals
            residual[:, poisson_channel] = y_poiss - mean_poiss
            residual[:, gaussian_channel] = (y_gauss - mean_gauss) / gauss_noise
            wadj = w[:, [l]]  # keep dimension
            GtWG = G.T @ (wadj * G)

            u = G @ (G.T @ (residual @ a[l, :])) - mu[:, l]
            try:
                M = solve(Ir + GtWG, (wadj * G).T @ u, assume_a='pos')
                delta_mu = u - G @ ((wadj * G).T @ u) + G @ (GtWG @ M)
                clip(delta_mu, dmu_bound)
            except Exception as e:
                logger.exception(repr(e), exc_info=True)
                delta_mu = 0

            dmu[:, l] = delta_mu
            mu[:, l] += delta_mu

        # TODO: remove duplicated computation
        eta = mu @ a + xb
        r = trunc_exp(eta + 0.5 * v @ (a ** 2))
        U[:, poisson_channel] = r[:, poisson_channel]
        U[:, gaussian_channel] = 1 / gauss_noise
        w = U @ (a.T ** 2)
        if method == "VB":
            for l in range(zdim):
                G = prior[l]
                GtWG = G.T @ (w[:, l, np.newaxis] * G)
                try:
                    M = solve(Ir + GtWG, GtWG, assume_a='pos')
                    v[:, l] = np.sum(G * (G - G @ GtWG + G @ (GtWG @ M)), axis=1)
                except Exception as e:
                    logger.exception(repr(e), exc_info=True)

        # make sure save all changes
        # TODO: make inline modification
    trial["mu"] = mu
    trial["w"] = w
    trial["v"] = v
    trial["dmu"] = dmu


def estep(trials, params, config):
    """Update variational distribution q (E step)"""
    for trial in trials:
        infer_single_trial(trial, params, config)


def mstep(trials, params, config):
    """Optimize loading and regression (M step)"""
    niter = config["Mniter"]  # maximum number of iterations
    if niter < 1:
        return

    # It's more proper to constrain the latent before mstep.
    # If the parameters are fixed, it's no need to optimize the posterior.
    # Besides, the constraint modifies the loading and bias.
    # constrain_latent(trials, params, config)

    # dimenionalities
    ydim = params["ydim"]
    xdim = params["xdim"]
    zdim = params["zdim"]
    rank = params["rank"]  # rank of prior covariance
    ntrial = len(trials)  # number of trials

    # parameters
    a = params["a"]
    b = params["b"]
    likelihood = params["likelihood"]
    noise = params["noise"]
    poiss_mask = likelihood == "poisson"
    gauss_mask = likelihood == "gaussian"
    gauss_noise = noise[gauss_mask]
    da = params["da"]
    db = params["db"]

    # misc
    use_hessian = config["use_hessian"]
    da_bound = config["da_bound"]
    db_bound = config["db_bound"]
    tol = config["tol"]
    method = config["method"]
    learning_rate = config["learning_rate"]

    y = np.concatenate([trial["y"] for trial in trials], axis=0)
    x = np.concatenate(
        [trial["x"] for trial in trials], axis=0
    )  # TODO: check dimensionality of x
    mu = np.concatenate([trial["mu"] for trial in trials], axis=0)
    v = np.concatenate([trial["v"] for trial in trials], axis=0)

    for i in range(niter):
        eta = mu @ a + einsum("ijk, jk -> ik", x, b)
        # (time, regression, neuron) x (regression, neuron) -> (time, neuron)  # TODO: use matmul broadcast
        r = trunc_exp(eta + 0.5 * v @ (a ** 2))
        noise = np.var(y - eta, axis=0, ddof=0)  # MLE

        for n in range(ydim):
            if likelihood[n] == "poisson":
                # loading
                mu_plus_v_times_a = mu + v * a[:, n]
                grad_a = mu.T @ y[:, n] - mu_plus_v_times_a.T @ r[:, n]

                if use_hessian:
                    nhess_a = mu_plus_v_times_a.T @ (
                        r[:, n, np.newaxis] * mu_plus_v_times_a
                    )
                    nhess_a[np.diag_indices_from(nhess_a)] += r[:, n] @ v

                    try:
                        jitter = np.diag(np.full_like(grad_a, fill_value=config["eps"]))
                        delta_a = solve(nhess_a + jitter, grad_a, assume_a='pos')
                    except Exception as e:
                        logger.exception(repr(e), exc_info=True)
                        delta_a = learning_rate * grad_a
                else:
                    delta_a = learning_rate * grad_a

                clip(delta_a, da_bound)
                da[:, n] = delta_a
                a[:, n] += delta_a

                # regression
                grad_b = x[..., n].T @ (y[:, n] - r[:, n])

                if use_hessian:
                    nhess_b = x[..., n].T @ (r[:, np.newaxis, n] * x[..., n])
                    try:
                        jitter = np.diag(np.full_like(grad_b, fill_value=config["eps"]))
                        delta_b = solve(nhess_b + jitter, grad_b, assume_a='pos')
                    except Exception as e:
                        logger.exception(repr(e), exc_info=True)
                        delta_b = learning_rate * grad_b
                else:
                    delta_b = learning_rate * grad_b

                clip(delta_b, db_bound)
                db[:, n] = delta_b
                b[:, n] += delta_b
            elif likelihood[n] == "gaussian":
                # a's least squares solution for Gaussian channel
                # (m'm + diag(j'v))^-1 m'(y - Hb)
                M = mu.T @ mu
                M[np.diag_indices_from(M)] += np.sum(v, axis=0)
                a[:, n] = solve(M, mu.T @ (y[:, n] - x[..., n] @ b[:, n]), assume_a='pos')

                # b's least squares solution for Gaussian channel
                # (H'H)^-1 H'(y - ma)
                b[:, n] = solve(
                    x[..., n].T @ x[..., n],
                    x[..., n].T @ (y[:, n] - mu @ a[:, n]),
                    assume_a='pos',
                )
                b[1:, n] = 0
                # TODO: only make history filter components zeros
            else:
                pass

        # update parameters in fit
        # TODO: make inline modification
        params["a"] = a
        params["b"] = b
        params["noise"] = noise
        # normalize loading by latent and rescale latent
        # constrain_a(model)

        # if norm(da) < tol * norm(a) and norm(db) < tol * norm(b):
        #     break


def hstep(trials, params, config):
    """Wrapper of hyperparameters tuning"""
    if not config["Hstep"]:
        return

    gp.optimize(trials, params, config)


def infer(trials, params, config):
    niter = config["Eniter"]
    config["Eniter"] = config["max_iter"]
    with timer() as elapsed:
        estep(trials, params, config)
    click.echo("{:.2f}s".format(elapsed()))
    config["Eniter"] = niter


def vem(trials, params, config):
    """Variational EM
    This function implements the algorithm.
    """
    # this function should not know if the trials are original or segmented ones
    # the caller determines which to use
    # pass segments to speed up estimation and hyperparameter tuning
    # the caller gets runtime

    callbacks = config["callbacks"]

    tol = config["tol"]
    niter = config["max_iter"]

    # profile and debug purpose
    # invalid every new run
    runtime = {
        "it": 0,
        "e_elapsed": [],
        "m_elapsed": [],
        "h_elapsed": [],
        "em_elapsed": [],
    }

    #######################
    # iterative algorithm #
    #######################

    # disable gabbage collection during the iterative procedure
    for it in range(niter):
        runtime["it"] += 1
        mu = np.concatenate([trial["mu"] for trial in trials], axis=0)
        a = params["a"]
        b = params["b"]
        norm_mu = norm(mu)
        norm_a = norm(a)
        norm_b = norm(b)

        with timer() as em_elapsed:
            ##########
            # E step #
            ##########
            with timer() as estep_elapsed:
                constrain_loading(trials, params, config)
                estep(trials, params, config)

            ##########
            # M step #
            ##########
            with timer() as mstep_elapsed:
                constrain_latent(trials, params, config)
                mstep(trials, params, config)

            ###################
            # H step #
            ###################
            with timer() as hstep_elapsed:
                hstep(trials, params, config)

        runtime["e_elapsed"].append(estep_elapsed())
        runtime["m_elapsed"].append(mstep_elapsed())
        runtime["h_elapsed"].append(hstep_elapsed())
        runtime["em_elapsed"].append(em_elapsed())

        config["runtime"] = runtime

        click.echo(
            "Iteration {:4d}, E-step {:.2f}s, M-step {:.2f}s".format(
                runtime["it"], runtime["e_elapsed"][-1], runtime["m_elapsed"][-1]
            )
        )

        for callback in callbacks:
            try:
                callback(trials, params, config)
            except RuntimeError:
                logger.error("Callback {} failed".format(callback))

        #####################
        # convergence check #
        #####################
        dmu = np.concatenate([trial["dmu"] for trial in trials], axis=0)
        da = params["da"]
        db = params["db"]

        converged = norm(dmu) < tol * norm_mu and norm(da) < tol * norm_a and norm(db) < tol * norm_b

        should_stop = converged and it + 1 >= config["min_iter"]

        if should_stop:
            break

    ##############################
    # end of iterative procedure #
    ##############################


def constrain_latent(trials, params, config):
    """Center and scale latent mean"""
    constraint = config["constrain_latent"]

    if not constraint or constraint == "none":
        return

    mu = np.concatenate([trial["mu"] for trial in trials], axis=0)
    mean_over_trials = mu.mean(axis=0, keepdims=True)
    std_over_trials = mu.std(axis=0, keepdims=True)

    if constraint in ("location", "both"):
        for trial in trials:
            trial["mu"] -= mean_over_trials
        # compensate bias
        # commented to isolated from changing external variables
        params["b"][0, :] += np.squeeze(mean_over_trials @ params["a"])

    if constraint in ("scale", "both"):
        for trial in trials:
            trial["mu"] /= std_over_trials
        # compensate loading
        # commented to isolated from changing external variables
        params["a"] *= std_over_trials.T


def constrain_loading(trials, params, config):
    """Normalize loading matrix"""
    constraint = config["constrain_loading"]

    if not constraint or constraint == "none":
        return

    eps = config["eps"]
    a = params["a"]

    if constraint == "svd":
        u, s, v = svd(a, full_matrices=False)
        # A = USV
        us = a @ v.T
        for trial in trials:
            trial["mu"] = trial["mu"] @ us
        params["a"] = v
    else:
        if constraint == "fro":
            s = norm(a, ord="fro") + eps
        else:
            s = norm(a, ord=constraint, axis=1, keepdims=True) + eps
        params["a"] /= s
        for trial in trials:
            trial["mu"] *= s.T


def update_w(trials, params, config):
    likelihood = params["likelihood"]
    poiss_mask = likelihood == "poisson"
    gauss_mask = likelihood == "gaussian"

    a = params["a"]
    b = params["b"]
    noise = params["noise"]
    gauss_noise = noise[gauss_mask]

    for trial in trials:
        y = trial["y"]
        x = trial["x"]
        mu = trial["mu"]
        w = trial.setdefault("w", np.zeros_like(mu))
        v = trial.setdefault("v", np.zeros_like(mu))

        # (neuron, time, regression) x (regression, neuron) -> (time, neuron)
        eta = mu @ a + einsum("ijk, jk -> ik", x, b)
        r = trunc_exp(eta + 0.5 * v @ (a ** 2))
        U = np.empty_like(r)
        U[:, poiss_mask] = r[:, poiss_mask]
        U[:, gauss_mask] = 1 / gauss_noise
        trial["w"] = U @ (a.T ** 2)


def update_v(trials, params, config):
    if config["method"] != "VB":
        return

    for trial in trials:
        zdim = params["zdim"]
        mu = trial["mu"]
        w = trial.setdefault("w", np.zeros_like(mu))
        v = trial.setdefault("v", np.zeros_like(mu))

        prior = params["cholesky"][mu.shape[0]]
        Ir = identity(prior[0].shape[-1])

        for l in range(zdim):
            G = prior[l]
            GtWG = G.T @ (w[:, [l]] * G)
            try:
                v[:, l] = np.sum(
                    G
                    * (
                        G - G @ GtWG + G @ (GtWG @ solve(Ir + GtWG, GtWG, assume_a='pos'))
                    ),
                    axis=1,
                )
            except LinAlgError:
                logger.error("Singular I + G'WG")
                # warnings.warn("Singular I + G'WG")


# class VLGP(Model):
#     def __init__(self, n_factors, random_state=0, **kwargs):
#         self.n_factors = n_factors
#         self.random_state = random_state
#         self._weight = None
#         self._bias = None
#         self.setup(**kwargs)
#
#     def fit(self, trials, **kwargs):
#         """Fit the vLGP model to data using vEM
#         :param trials: list of trials
#         :return: the trials containing the latent factors
#         """
#         config = get_config(**kwargs)
#
#         # add built-in callbacks
#         callbacks = config["callbacks"]
#         if "path" in config:
#             saver = Saver()
#             callbacks.extend([show, saver.save])
#         config["callbacks"] = callbacks
#
#         params = get_params(trials, self.n_factors, **kwargs)
#
#         click.echo("Initializing...")
#         initialize(trials, params, config)
#
#         # fill arrays
#         fill_params(params)
#
#         fill_trials(trials)
#         make_cholesky(trials, params, config)
#         update_w(trials, params, config)
#         update_v(trials, params, config)
#
#         subtrials = cut_trials(trials, params, config)
#         make_cholesky(subtrials, params, config)
#
#         fill_trials(subtrials)
#
#         params["initial"] = copy.deepcopy(params)
#         # VEM
#         click.echo("Fitting...")
#         vem(subtrials, params, config)
#         # E step only for inference given above estimated parameters and hyperparameters
#         make_cholesky(trials, params, config)
#         update_w(trials, params, config)
#         update_v(trials, params, config)
#         click.echo("Inferring...")
#         infer(trials, params, config)
#         click.echo("Done")
#
#         self._weight = params["a"]
#         self._bias = params["b"]
#
#         return trials
#
#     def infer(self, trials):
#         if not self.isfiited:
#             raise ValueError(
#                 "This model is not fitted yet. Call 'fit' with "
#                 "appropriate arguments before this method."
#             )
#         raise NotImplementedError()
#
#     def __eq__(self, other):
#         if (
#             isinstance(other, VLGP)
#             and self.n_factors == other.n_factors
#             and np.array_equal(self.weight, other.weight)
#             and np.array_equal(self.bias, other.bias)
#         ):
#             return True
#         return False
#
#     def setup(self, **kwargs):
#         pass
#
#     @property
#     def isfitted(self):
#         return self.weight is not None
#
#     @property
#     def weight(self):
#         return self._weight
#
#     @property
#     def bias(self):
#         return self._bias


def fast_estep(y, z, xB, C, d, K, *, max_iter):
    # assume equal length
    # assume Poisson
    # MAP estimation
    if max_iter < 1:
        return

    ydim = y.shape[-1]
    zdim = z.shape[-1]

    # reshape
    y = y.T.reshape(-1, 1)
    z = z.T.reshape(-1, 1)
    xB = xB + d[None, :]
    xB = xB.T.reshape(-1, 1)
    bigC = np.kron(C.T, np.eye(ydim))
    bigK = np.kron(np.eye(zdim), K)

    for i in range(max_iter):
        lam = trunc_exp(xB + bigC @ z)
        grad = bigC @ (y - lam) - solve(bigK, z)
        A = diag(1 / lam) - np.linalg.multi_dot((bigC, bigK, bigC.T))
        Hi = np.linalg.multi_dot((bigK, bigC.T, solve(A, bigC), bigK))
        dz = Hi @ grad
        z += dz
    return z


def fast_mstep(y, z, x, B, C, d, K, *, max_iter):
    # assume equal length
    # assume Poisson
    # MAP estimation
    if max_iter < 1:
        return

    ydim = y.shape[-1]
    zdim = z.shape[-1]

    # X = concatenate((x, z, 1), axis=-1)
    # b = concatenate((B, C, d), axis=0)

    # for i in range(max_iter):
    #     lam = exp(X @ b)
    #     grad = (y - lam) @ X
    #     H = multi_dot((X.T, diag(lam), X))
    #     b -= solve(H, grad)


def diag(a):
    if a.ndim > 1:
        return np.stack([np.diag(v) for v in a])
    else:
        return np.diag(a)

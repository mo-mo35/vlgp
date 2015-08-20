import itertools
import warnings
import time
import numpy as np
from util import history


def likelihood(y, x, a, b, intercept=True):
    T, N = y.shape
    L, _ = x.shape
    k, _ = b.shape
    p = (k - intercept) // N

    Y = history(y, p, intercept)

    lograte = np.dot(Y, b) + np.dot(x, a)
    return np.sum(y * lograte - np.exp(lograte))


def saferate(t, n, Y, m, V, b, a):
    lograte = np.dot(Y[t, :], b[:, n]) + np.dot(m[t, :], a[:, n]) + 0.5 * np.sum(a[:, n] * a[:, n] * V[:, t, t])
    rate = np.nan_to_num(np.exp(lograte))
    return rate if rate > 0 else np.finfo(np.float).eps


def lowerbound(y, b, a, mu, omega, m, V, complete=False, Y=None, rate=None):
    """
    Calculate the lower bound
    :param y: (T, N), spike trains
    :param b: (1 + p*N, N), coefficients of y
    :param a: (L, N), coefficients of x
    :param mu: (T, L), prior mean
    :param omega: (L, T, T), prior inverse covariances
    :param m: (T, L), latent posterior mean
    :param V: (L, T, T), latent posterior covariances
    :param complete: compute constant terms
    :param Y: (T, 1 + p*N), vectorized spike history
    :param rate: (T, N), E(E(y|x))
    :return lbound: lower bound
    """

    _, L = mu.shape
    T, N = y.shape

    lbound = np.sum(y * (np.dot(Y, b) + np.dot(m, a)) - rate)

    for l in range(L):
        lbound += -0.5 * np.dot(m[:, l] - mu[:, l], np.dot(omega[l, :, :], m[:, l] - mu[:, l])) + \
                  -0.5 * np.trace(np.dot(omega[l, :, :], V[l, :, :])) + 0.5 * np.linalg.slogdet(V[l, :, :])[1]

    return lbound + 0.5 * np.sum(np.linalg.slogdet(omega)[1]) if complete else lbound

default_control = {'maxiter': 200,
                   'inneriter': 5,
                   'tol': 1e-4,
                   'verbose': False}

def variational(y, p, mu, sigma, omega=None,
                a0=None, b0=None, m0=None, V0=None, K0=None,
                fixa=False, fixb=False, fixm=False, fixV=False, anorm=1.0, intercept=True,
                constrain_m='lag', constrain_a='lag',
                control=default_control):
    """
    :param y: (T, N), spike trains
    :param mu: (T, L), prior mean
    :param sigma: (L, T, T), prior covariance
    :param omega: (L, T, T), inverse prior covariance
    :param p: order of autoregression
    :param maxiter: maximum number of iterations
    :param tol: convergence tolerance
    :return
        m: posterior mean
        V: posterior covariance
        b: coefficients of y
        a: coefficients of x
        lbound: lower bound sequence
        it: number of iterations
    """
    start = time.time()  # time when algorithm starts

    def updaterate(t, n):
        # rate = E(E(y|x))
        for t, n in itertools.product(t, n):
            rate[t, n] = saferate(t, n, Y, m, V, b, a)

    # control
    maxiter = control['maxiter']
    inneriter = control['inneriter']
    tol = control['tol']
    verbose = control['verbose']

    # epsilon
    eps = 2 * np.finfo(np.float).eps

    # dimensions
    T, N = y.shape
    _, L = mu.shape

    eyeL = np.identity(L)
    eyeN = np.identity(N)
    eyeT = np.identity(T)
    oneT = np.ones(T)
    jayT = np.ones((T, T))
    oneTL = np.ones((T, L))

    # calculate inverse of prior covariance if not given
    if omega is None:
        omega = np.empty_like(sigma)
        for l in range(L):
            omega[l, :, :] = np.linalg.inv(sigma[l, :, :])

    # read-only variables, protection from unexpected assignment
    y.setflags(write=0)
    mu.setflags(write=0)
    sigma.setflags(write=0)
    omega.setflags(write=0)

    # construct history
    Y = history(y, p, intercept)

    # initialize args
    # make a copy to avoid changing initial values
    if m0 is None:
        m = mu.copy()
    else:
        m = m0.copy()

    if V0 is None:
        V = sigma.copy()
    else:
        V = V0.copy()

    if K0 is None:
        K = omega.copy()
    else:
        K = np.empty_like(V)
        for l in range(L):
            K[l, :, :] = np.linalg.inv(V[l, :, :])

    if a0 is None:
        a0 = np.random.randn(L, N)
        a0 /= np.linalg.norm(a0) / anorm
    a = a0.copy()

    if b0 is None:
        b0 = np.linalg.lstsq(Y, y)[0]
    b = b0.copy()

    # initialize rate matrix, rate = E(E(y|x))
    rate = np.empty_like(y)
    updaterate(range(T), range(N))

    # initialize lower bound
    lbound = np.full(maxiter, np.NINF)
    lbound[0] = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)

    # old values
    old_a = a.copy()
    old_b = b.copy()
    old_m = m.copy()
    old_V = V.copy()

    # variables for recovery
    last_b = b.copy()
    last_a = a.copy()
    last_m = m.copy()
    last_rate = rate.copy()
    last_V = np.empty((T, T))

    ra = np.ones(N)
    rb = np.ones(N)
    rm = np.ones(L)
    dec = 0.5
    inc = 1.5
    thld = 0.75

    # gradient and hessian
    grad_a_lag = np.zeros(N + 1)
    hess_a_lag = np.zeros((grad_a_lag.size, grad_a_lag.size))
    lam_a = np.zeros(L)
    lam_last_a = lam_a.copy()

    grad_m_lag = np.zeros(T + 1)
    hess_m_lag = np.zeros((grad_m_lag.size, grad_m_lag.size))
    lam_m = np.zeros(L)
    lam_last_m = lam_m.copy()

    it = 1
    convergent = False
    while not convergent and it < maxiter:
        if not fixb:
            for n in range(N):
                grad_b = np.dot(Y.T, y[:, n] - rate[:, n])
                hess_b = np.dot(Y.T, (Y.T * -rate[:, n]).T)
                if np.linalg.norm(grad_b, ord=np.inf) < eps:
                    break
                try:
                    delta_b = -rb[n] * np.linalg.solve(hess_b, grad_b)
                except np.linalg.LinAlgError as e:
                    print('b', e)
                    continue
                last_b[:, n] = b[:, n]
                last_rate[:, n] = rate[:, n]
                predict = np.inner(grad_b, delta_b) + 0.5 * np.dot(delta_b, np.dot(hess_b, delta_b))
                b[:, n] += delta_b
                updaterate(range(T), [n])
                lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                if np.isnan(lb) or lb < lbound[it - 1]:
                    rb[n] = dec * rb[n] + eps
                    b[:, n] = last_b[:, n]
                    rate[:, n] = last_rate[:, n]
                elif lb - lbound[it - 1] > thld * predict:
                    rb[n] *= inc
                    # if rb[n] > 1:
                    #     rb[n] = 1.0

        if not fixa:
            for l in range(L):
                grad_a = np.dot((y - rate).T, m[:, l]) - np.dot(rate.T, V[l, :, :].diagonal()) * a[l, :]
                hess_a = -np.diag(np.dot(rate.T, m[:, l] * m[:, l])
                                  + 2 * np.dot(rate.T, m[:, l] * V[l, :, :].diagonal()) * a[l, :]
                                  + np.dot(rate.T, V[l, :, :].diagonal() ** 2) * a[l, :] ** 2
                                  + np.dot(rate.T, V[l, :, :].diagonal()))
                if constrain_a == 'lag':
                    grad_a_lag[:N] = grad_a + 2 * lam_a[l] * a[l, :]
                    grad_a_lag[N:] = np.inner(a[l, :], a[l, :]) - anorm ** 2
                    hess_a_lag[:N, :N] = hess_a + 2 * lam_a[l] * eyeN
                    hess_a_lag[N:, :N] = 2 * a[l, :]
                    hess_a_lag[:N, N:] = hess_a_lag[N:, :N].T
                    hess_a_lag[N:, N:] = 0
                    if np.linalg.norm(grad_a_lag, ord=np.inf) < eps:
                        break
                    try:
                        delta_a_lag = -ra[l] * np.linalg.solve(hess_a_lag, grad_a_lag)
                    except np.linalg.LinAlgError as e:
                        print('a', e)
                        continue
                    lam_last_a[l] = lam_a[l]
                    last_a[l, :] = a[l, :]
                    last_rate[:] = rate[:]
                    predict = np.inner(grad_a_lag, delta_a_lag) + 0.5 * np.dot(delta_a_lag, np.dot(hess_a_lag, delta_a_lag))
                    a[l, :] += delta_a_lag[:N]
                    lam_a[l] += delta_a_lag[N:]
                    updaterate(range(T), range(N))
                    lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                    if np.isnan(lb) or lb - lbound[it - 1] < 0:
                        ra[l] *= dec
                        ra[l] += eps
                        a[l, :] = last_a[l, :]
                        rate[:] = last_rate[:]
                        lam_a[l] = lam_last_a[l]
                    elif lb - lbound[it - 1] > thld * predict:
                        ra[l] *= inc
                        # if ra[l] > 1:
                        #     ra[l] = 1.0
                else:
                    if np.linalg.norm(grad_a, ord=np.inf) < eps:
                        break
                    try:
                        delta_a = -ra[l] * np.linalg.solve(hess_a, grad_a)
                    except np.linalg.LinAlgError as e:
                        print('a', e)
                        continue
                    last_a[l, :] = a[l, :]
                    last_rate[:] = rate[:]
                    predict = np.inner(grad_a, delta_a) + 0.5 * np.dot(delta_a, np.dot(hess_a, delta_a))
                    a[l, :] += delta_a
                    updaterate(range(T), range(N))
                    lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                    if np.isnan(lb) or lb - lbound[it - 1] < 0:
                        ra[l] *= dec
                        ra[l] += eps
                        a[l, :] = last_a[l, :]
                        rate[:] = last_rate[:]
                    elif lb - lbound[it - 1] > thld * predict:
                        ra[l] *= inc
                        # if ra[l] > 1:
                        #     ra[l] = 1.0
                    if np.linalg.norm(a[l, :]) > 0:
                        a[l, :] /= np.linalg.norm(a[l, :]) / anorm

        # posterior mean
        if not fixm:
            for l in range(L):
                grad_m = np.dot(y - rate, a[l, :]) - np.dot(omega[l, :, :], m[:, l] - mu[:, l])
                hess_m = np.diag(np.dot(-rate, a[l, :] * a[l, :])) - omega[l, :, :]
                if constrain_m == 'lag':
                    grad_m_lag[:T] = grad_m + lam_m[l]
                    grad_m_lag[T:] = np.sum(m[:, l])
                    hess_m_lag[:T, :T] = hess_m
                    hess_m_lag[:T, T:] = hess_m_lag[T:, :T] = 1
                    hess_m_lag[T:, T:] = 0

                    if np.linalg.norm(grad_m_lag, ord=np.inf) < eps:
                        break
                    try:
                        delta_m_lag = -rm[l] * np.linalg.solve(hess_m_lag, grad_m_lag)
                    except np.linalg.LinAlgError as e:
                        print('m', e)
                        continue
                    last_m[:, l] = m[:, l]
                    last_rate[:] = rate
                    lam_last_m[l] = lam_m[l]
                    predict = np.inner(grad_m_lag, delta_m_lag) + 0.5 * np.dot(delta_m_lag, np.dot(hess_m_lag, delta_m_lag))
                    m[:, l] += delta_m_lag[:T]
                    lam_m[l] += delta_m_lag[T:]
                    updaterate(range(T), range(N))
                    lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                    if np.isnan(lb) or lb < lbound[it - 1]:
                        rm[l] *= dec
                        rm[l] += eps
                        m[:, l] = last_m[:, l]
                        rate[:] = last_rate
                        lam_m[l] = lam_last_m[l]
                    elif lb - lbound[it - 1] > thld * predict:
                        rm[l] *= inc
                        # if rm[l] > 1:
                        #     rm[l] = 1.0
                else:
                    if np.linalg.norm(grad_m, ord=np.inf) < eps:
                        break
                    try:
                        delta_m = -rm[l] * np.linalg.solve(hess_m, grad_m)
                    except np.linalg.LinAlgError as e:
                        print('m', e)
                        continue
                    last_m[:, l] = m[:, l]
                    last_rate[:] = rate
                    predict = np.inner(grad_m, delta_m) + 0.5 * np.dot(delta_m, np.dot(hess_m, delta_m))
                    m[:, l] += delta_m
                    updaterate(range(T), range(N))
                    lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                    if np.isnan(lb) or lb < lbound[it - 1]:
                        rm[l] *= dec
                        rm[l] += eps
                        m[:, l] = last_m[:, l]
                        rate[:] = last_rate
                    elif lb - lbound[it - 1] > thld * predict:
                        rm[l] *= inc
                        # if rm[l] > 1:
                        #     rm[l] = 1.0
                    m[:, l] -= np.mean(m[:, l])

        # posterior covariance
        if not fixV:
            for l in range(L):
                for t in range(T):
                    last_rate[t, :] = rate[t, :]
                    last_V[:] = V[l, :, :]
                    k_ = K[l, t, t] - 1 / V[l, t, t]  # \tilde{k}_tt
                    old_vtt = V[l, t, t]
                    # fixed point iterations
                    for _ in range(inneriter):
                        V[l, t, t] = 1 / (omega[l, t, t] - k_ + np.sum(rate[t, :] * a[l, :] * a[l, :]))
                        updaterate([t], range(N))
                    # update V
                    not_t = np.arange(T) != t
                    V[np.ix_([l], not_t, not_t)] = V[np.ix_([l], not_t, not_t)] \
                                                   + (V[l, t, t] - old_vtt) \
                                                   * np.outer(V[l, t, not_t], V[l, t, not_t]) / (old_vtt * old_vtt)
                    V[l, t, not_t] = V[l, not_t, t] = V[l, t, t] * V[l, t, not_t] / old_vtt
                    # update k_tt
                    K[l, t, t] = k_ + 1 / V[l, t, t]
                    # lb = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)
                    # if np.isnan(lb) or lb < lbound[it - 1]:
                    #     print('V[{}] decreased L.'.format(l))
                        # V[l, :, :] = last_V
                        # K[l, t, t] = k_ + 1 / V[l, t, t]
                        # rate[t, :] = last_rate[t, :]

        # update lower bound
        lbound[it] = lowerbound(y, b, a, mu, omega, m, V, Y=Y, rate=rate)

        # check convergence
        del_a = 0.0 if fixa else np.max(np.abs(old_a - a))
        del_b = 0.0 if fixb or p == 0 else np.max(np.abs(old_b - b))
        # del_c = 0.0 if fixc else np.max(np.abs(old_c - c))
        del_m = 0.0 if fixm else np.max(np.abs(old_m - m))
        del_V = 0.0 if fixV else np.max(np.abs(old_V - V))
        delta = max(del_a, del_b, del_m, del_V)

        if delta < tol:
            convergent = True

        if verbose:
            print('\nIteration[%d]: L = %.5f, inc = %.10f' %
                  (it + 1, lbound[it], lbound[it] - lbound[it - 1]))
            print('delta alpha = %.10f' % del_a)
            print('delta beta = %.10f' % del_b)
            # print('delta gamma = %.10f' % del_c)
            print('delta m = %.10f' % del_m)
            print('delta V = %.10f' % del_V)

        old_a[:] = a
        old_b[:] = b
        # old_c[:] = c
        old_m[:] = m
        old_V[:] = V

        it += 1

    if it == maxiter:
        warnings.warn('not convergent', RuntimeWarning)

    stop = time.time()

    return m, V, a, b, a0, b0, lbound[:it], stop - start, convergent

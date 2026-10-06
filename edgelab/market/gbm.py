"""Gradient-boosted decision trees in numpy (ADR-106): no extra library, deterministic (fixed seed), small.

Histogram trees: every feature is cut into <= 32 bins at quantiles of the TRAINING data; trees are grown level by
level to a fixed depth; split gain and leaf values use the second-order (Newton) formulas with L2 regularisation;
rows and features are subsampled per tree (seeded). Losses: 'logloss' (probability of +1) and 'l2' (a number)."""
from __future__ import annotations

import numpy as np


class GBM:
    def __init__(self, loss: str = "logloss", n_trees: int = 100, depth: int = 3, lr: float = 0.06,
                 min_leaf: int = 150, bins: int = 32, subsample: float = 0.5, colsample: float = 0.7,
                 l2: float = 5.0, seed: int = 0):
        assert loss in ("logloss", "l2")
        self.loss, self.n_trees, self.depth, self.lr = loss, n_trees, depth, lr
        self.min_leaf, self.bins, self.subsample, self.colsample, self.l2, self.seed = \
            min_leaf, bins, subsample, colsample, l2, seed
        self.edges: list[np.ndarray] = []
        self.trees: list[tuple] = []
        self.f0 = 0.0
        self.gain = None

    # ------------------------------------------------------------------ binning
    def _fit_bins(self, X: np.ndarray) -> None:
        self.edges = []
        qs = np.linspace(0, 1, self.bins + 1)[1:-1]
        for j in range(X.shape[1]):
            col = X[:, j]
            col = col[np.isfinite(col)]
            e = np.unique(np.quantile(col, qs)) if len(col) else np.zeros(0)
            self.edges.append(e)

    def _bin(self, X: np.ndarray) -> np.ndarray:
        out = np.empty(X.shape, dtype=np.uint8)
        for j, e in enumerate(self.edges):
            col = np.nan_to_num(X[:, j], nan=-np.inf)
            out[:, j] = np.searchsorted(e, col, side="right")
        return out

    # ------------------------------------------------------------------ fitting
    def fit(self, X: np.ndarray, y: np.ndarray) -> "GBM":
        X = np.asarray(X, float)
        y = np.asarray(y, float)
        n, F = X.shape
        rng = np.random.default_rng(self.seed)
        self._fit_bins(X)
        Xb = self._bin(X)
        B = self.bins
        if self.loss == "logloss":
            p = np.clip(y.mean(), 1e-4, 1 - 1e-4)
            self.f0 = float(np.log(p / (1 - p)))
        else:
            self.f0 = float(y.mean())
        Fx = np.full(n, self.f0)
        self.trees = []
        self.gain = np.zeros(F)
        for t in range(self.n_trees):
            if self.loss == "logloss":
                pr = 1 / (1 + np.exp(-Fx))
                g, h = pr - y, np.maximum(pr * (1 - pr), 1e-6)
            else:
                g, h = Fx - y, np.ones(n)
            rows = np.flatnonzero(rng.random(n) < self.subsample)
            cols = np.flatnonzero(rng.random(F) < self.colsample)
            if not len(cols):
                cols = np.array([rng.integers(F)])
            tree = self._grow(Xb[rows][:, cols], g[rows], h[rows], cols, B)
            Fx += self.lr * self._apply(tree, Xb)
            self.trees.append(tree)
        return self

    def _grow(self, Xb, g, h, cols, B):
        n, F = Xb.shape
        node = np.zeros(n, dtype=np.int64)
        splits = []                                         # per level: (feature (global), threshold bin) per node
        for level in range(self.depth):
            nn = 2 ** level
            key = (node[:, None] * F + np.arange(F)[None, :]) * B + Xb.astype(np.int64)
            size = nn * F * B
            G = np.bincount(key.ravel(), weights=np.repeat(g, F), minlength=size).reshape(nn, F, B)
            H = np.bincount(key.ravel(), weights=np.repeat(h, F), minlength=size).reshape(nn, F, B)
            C = np.bincount(key.ravel(), minlength=size).reshape(nn, F, B)
            GL, HL, CL = np.cumsum(G, 2), np.cumsum(H, 2), np.cumsum(C, 2)
            Gt, Ht, Ct = GL[:, :, -1:], HL[:, :, -1:], CL[:, :, -1:]
            GR, HR, CR = Gt - GL, Ht - HL, Ct - CL
            lam = self.l2
            gain = GL ** 2 / (HL + lam) + GR ** 2 / (HR + lam) - Gt ** 2 / (Ht + lam)
            gain[(CL < self.min_leaf) | (CR < self.min_leaf)] = -np.inf
            lvl = []
            for k in range(nn):
                flat = np.argmax(gain[k])
                f, b = divmod(int(flat), B)
                if not np.isfinite(gain[k, f, b]) or gain[k, f, b] <= 0:
                    lvl.append((-1, 0))                      # no split: everything goes left
                    continue
                lvl.append((int(cols[f]), b))
                self.gain[cols[f]] += float(gain[k, f, b])
            # children: left = 2k, right = 2k+1
            new = node * 2
            for k, (fg, b) in enumerate(lvl):
                if fg < 0:
                    continue
                f = int(np.flatnonzero(cols == fg)[0])
                new[(node == k) & (Xb[:, f] > b)] += 1
            node = new
            splits.append(lvl)
        leaves = 2 ** self.depth
        Gs = np.bincount(node, weights=g, minlength=leaves)
        Hs = np.bincount(node, weights=h, minlength=leaves)
        values = -Gs / (Hs + self.l2)
        return splits, values

    def _apply(self, tree, Xb_full) -> np.ndarray:
        splits, values = tree
        node = np.zeros(len(Xb_full), dtype=np.int64)
        for lvl in splits:
            new = node * 2
            for k, (f, b) in enumerate(lvl):
                if f < 0:
                    continue
                new[(node == k) & (Xb_full[:, f] > b)] += 1
            node = new
        return values[node]

    def raw(self, X: np.ndarray) -> np.ndarray:
        Xb = self._bin(np.asarray(X, float))
        out = np.full(len(Xb), self.f0)
        for tree in self.trees:
            out += self.lr * self._apply(tree, Xb)
        return out

    def predict(self, X: np.ndarray) -> np.ndarray:
        r = self.raw(X)
        return 1 / (1 + np.exp(-r)) if self.loss == "logloss" else r


class Logistic:
    """L2-regularised logistic regression (Newton steps) on standardised features."""

    def __init__(self, l2: float = 1.0, iters: int = 25):
        self.l2, self.iters = l2, iters

    def fit(self, X, y):
        X = np.asarray(X, float)
        self.mu = np.nanmean(X, 0)
        self.sd = np.nanstd(X, 0)
        self.sd[~(self.sd > 0)] = 1
        Z = np.nan_to_num((X - self.mu) / self.sd)
        Z = np.c_[np.ones(len(Z)), Z]
        w = np.zeros(Z.shape[1])
        reg = np.full(Z.shape[1], self.l2)
        reg[0] = 0
        for _ in range(self.iters):
            p = 1 / (1 + np.exp(-Z @ w))
            g = Z.T @ (p - y) + reg * w
            Hm = (Z * (p * (1 - p))[:, None]).T @ Z + np.diag(reg + 1e-9)
            step = np.linalg.solve(Hm, g)
            w -= step
            if np.abs(step).max() < 1e-7:
                break
        self.w = w
        return self

    def predict(self, X):
        Z = np.nan_to_num((np.asarray(X, float) - self.mu) / self.sd)
        return 1 / (1 + np.exp(-(self.w[0] + Z @ self.w[1:])))

    def contributions(self, X):
        Z = np.nan_to_num((np.asarray(X, float) - self.mu) / self.sd)
        return Z * self.w[1:]


class Ridge:
    def __init__(self, l2: float = 10.0):
        self.l2 = l2

    def fit(self, X, y):
        X = np.asarray(X, float)
        self.mu = np.nanmean(X, 0)
        self.sd = np.nanstd(X, 0)
        self.sd[~(self.sd > 0)] = 1
        Z = np.nan_to_num((X - self.mu) / self.sd)
        self.b0 = float(np.mean(y))
        A = Z.T @ Z + self.l2 * np.eye(Z.shape[1])
        self.w = np.linalg.solve(A, Z.T @ (y - self.b0))
        return self

    def predict(self, X):
        Z = np.nan_to_num((np.asarray(X, float) - self.mu) / self.sd)
        return self.b0 + Z @ self.w

"""
gemini_retry.py
===============
Shared retry, rate-limiting, model-switching, and **daily quota enforcement**
utilities for all Gemini API calls in the Chief-of-Staff pipeline.

Free-tier Gemini API quotas (as of mid-2026)
---------------------------------------------
  gemini-2.5-flash    20  requests/day,  20  requests/minute
  gemini-2.0-flash    1500 requests/day, 30  requests/minute
  gemini-1.5-flash    1500 requests/day, 30  requests/minute

What this module provides:
  1. ``DailyQuota`` — persists per-model daily usage to a JSON file so the
     quota is tracked across process restarts.
  2. ``call_with_retry()`` — wraps a Gemini API call, catches 429 /
     resource-exhausted errors, retries with exponential backoff, and
     **fast-fails** when the daily quota is exhausted.
  3. ``RateLimiter`` — sleeps between calls to stay under per-minute quotas.
  4. ``resolve_model()`` — picks a model from env / code default.
  5. ``check_daily_quota()`` — convenience function for callers to know
     whether they can still make API calls today.
"""

from __future__ import annotations

import json
import os
import time
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

logger = logging.getLogger("gemini_retry")

# ---------------------------------------------------------------------------
# Model catalogue  (model_id -> (requests_per_day, requests_per_minute))
# ---------------------------------------------------------------------------
_FREE_TIER_MODELS: dict[str, tuple[int, int]] = {
    "gemini-2.5-flash":  (20,   20),
    "gemini-2.0-flash":  (1500, 30),
    "gemini-1.5-flash":  (1500, 30),
}

# Where we persist daily usage counters so they survive restarts.
_QUOTA_FILE = Path(__file__).resolve().parent / ".gemini_daily_usage.json"

# ---------------------------------------------------------------------------
# Persistent daily quota tracker
# ---------------------------------------------------------------------------

class DailyQuota:
    """Thread-safe (coarse) daily usage counter persisted to disk.

    Tracks ``{model_name: count}`` for the current calendar day (UTC).
    The counter resets automatically when the date changes.

    Usage::

        quota = DailyQuota()
        if quota.remaining("gemini-2.0-flash") > 0:
            quota.increment("gemini-2.0-flash")
            # … make API call …

    The data file is at ``<project_root>/.gemini_daily_usage.json``.
    """

    def __init__(self, quota_file: str | Path = _QUOTA_FILE) -> None:
        self._path = Path(quota_file)
        self._data: dict[str, Any] = self._load()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _today_key(self) -> str:
        """Return the date string used as the top-level key (e.g. '2026-06-23')."""
        return date.today().isoformat()

    def _ensure_today(self) -> dict[str, int]:
        """Return the ``{model: count}`` dict for today, resetting if stale."""
        today = self._today_key()
        if self._data.get("date") != today:
            self._data = {"date": today, "models": {}}
            self._save()
        return self._data.setdefault("models", {})

    def _load(self) -> dict[str, Any]:
        try:
            with self._path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {"date": self._today_key(), "models": {}}

    def _save(self) -> None:
        try:
            with self._path.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
        except OSError:
            logger.warning("Failed to write quota file at %s", self._path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count(self, model: str) -> int:
        """Return how many calls have been made to *model* today."""
        counts = self._ensure_today()
        return counts.get(model, 0)

    def remaining(self, model: str) -> int:
        """Return how many free-tier calls remain today for *model*.

        If *model* is not in our free-tier catalogue, returns a large
        number (we assume a paid tier with no hard cap).
        """
        limit = _FREE_TIER_MODELS.get(model)
        if limit is None:
            return 999_999  # unknown model → assume paid, no daily cap
        daily_limit = limit[0]
        used = self.count(model)
        return max(0, daily_limit - used)

    def increment(self, model: str) -> int:
        """Increment the counter for *model* and save. Returns new count."""
        counts = self._ensure_today()
        current = counts.get(model, 0)
        counts[model] = current + 1
        self._save()
        return counts[model]

    def reset(self, model: str | None = None) -> None:
        """Reset counters for today (for testing / manual override).

        If *model* is None, clears ALL counters for today.
        """
        counts = self._ensure_today()
        if model:
            counts.pop(model, None)
        else:
            self._data["models"] = {}
        self._save()

    def summary(self) -> dict[str, dict[str, int]]:
        """Return a human-friendly usage summary::

            {
                "gemini-2.0-flash": {"used": 3, "limit": 1500, "remaining": 1497},
                ...
            }
        """
        counts = self._ensure_today()
        result: dict[str, dict[str, int]] = {}
        for model, (limit, _rpm) in _FREE_TIER_MODELS.items():
            used = counts.get(model, 0)
            result[model] = {
                "used": used,
                "limit": limit,
                "remaining": max(0, limit - used),
            }
        return result


# ---------------------------------------------------------------------------
# Singleton — all modules in the same process share this instance.
# ---------------------------------------------------------------------------
_DAILY_QUOTA = DailyQuota()


def check_daily_quota(model: str | None = None) -> dict:
    """Convenience: return usage summary for *model* (or all models if None).

    Example return::

        {"model": "gemini-2.0-flash", "used": 3, "limit": 1500, "remaining": 1497}

    If *model* is None, returns the full summary dict.
    """
    if model:
        remaining = _DAILY_QUOTA.remaining(model)
        limit = _FREE_TIER_MODELS.get(model, (0, 0))[0]
        return {
            "model": model,
            "used": _DAILY_QUOTA.count(model),
            "limit": limit,
            "remaining": remaining,
        }
    return _DAILY_QUOTA.summary()


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple token-bucket rate limiter.

    Ensures we never exceed ``requests_per_minute`` calls.  Sleeps the
    current thread when the bucket is empty.
    """

    def __init__(self, requests_per_minute: int) -> None:
        self._min_interval = 60.0 / max(requests_per_minute, 1)
        self._last_call: float = 0.0

    def wait(self) -> None:
        """Sleep if needed to respect the rate limit."""
        now = time.monotonic()
        elapsed = now - self._last_call
        if elapsed < self._min_interval:
            sleep_for = self._min_interval - elapsed
            logger.debug("RateLimiter sleeping %.2fs", sleep_for)
            time.sleep(sleep_for)
        self._last_call = time.monotonic()


# ---------------------------------------------------------------------------
# Retry wrapper
# ---------------------------------------------------------------------------

T = TypeVar("T")


def _quota_is_daily_exhausted(exc: Exception) -> bool:
    """Check if the error indicates daily quota = 0 (not just per-minute)."""
    msg = str(exc)
    # Look for "limit: 0" next to "per day" or "per project per model"
    return "limit: 0" in msg and (
        "per_day" in msg.lower()
        or "dayperproject" in msg.lower().replace(" ", "")
    )


def call_with_retry(
    fn: Callable[..., T],
    *args: Any,
    max_retries: int = 5,
    base_delay: float = 2.0,
    model_name: str | None = None,
    **kwargs: Any,
) -> T:
    """Call ``fn(*args, **kwargs)`` and retry on 429 / resource-exhausted.

    **NEW**: Before making the call, this function checks the daily quota
    for *model_name*. If the quota is exhausted, it raises ``RuntimeError``
    immediately — no API call is made. This is the key mechanism that
    prevents your daily free limit from being exceeded.

    Retry strategy:
      - If the **daily** quota is exhausted (limit: 0 for per-day metric),
        fail immediately with a clear message — no point retrying.
      - Otherwise: exponential back-off: base_delay * (2 ** attempt)
      - Jitter: ±25% random delay to avoid thundering-herd
      - Stops after ``max_retries`` attempts and re-raises the last error.

    Parameters
    ----------
    fn
        The callable to invoke (e.g. ``model.generate_content``).
    max_retries
        Max number of *retries* (attempts = max_retries + 1).  Default 5.
    base_delay
        Initial delay in seconds.  Doubles each retry.  Default 2.0.
    model_name
        The model name being used (e.g. ``"gemini-2.0-flash"``).  If
        provided, we check the daily quota before calling.  If ``None``,
        quota checks are skipped (allows callers that don't have model
        info to still use the retry wrapper).
    """
    import random

    # ---------- PRE-FLIGHT DAILY QUOTA CHECK ----------
    if model_name is not None:
        remaining = _DAILY_QUOTA.remaining(model_name)
        if remaining <= 0:
            limit = _FREE_TIER_MODELS.get(model_name, (0, 0))[0]
            raise RuntimeError(
                f"🚫 Daily free-tier quota exhausted for '{model_name}'.\n"
                f"  • Used: {_DAILY_QUOTA.count(model_name)} / {limit}\n"
                f"  • Resets at midnight Pacific Time (≈12:30 PM IST).\n"
                f"  • Get a new API key at https://aistudio.google.com/apikey\n"
                f"  • Or enable billing at https://console.cloud.google.com/apis"
            )

    last_exc: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            # ---------- INCREMENT QUOTA ON SUCCESS ----------
            result = fn(*args, **kwargs)
            if model_name is not None:
                _DAILY_QUOTA.increment(model_name)
            return result
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc).lower()

            # Only retry on 429 / resource-exhausted / quota errors.
            if "429" not in exc_str and "resource_exhausted" not in exc_str and "quota" not in exc_str:
                raise  # not a rate-limit error → propagate immediately

            # ---------- FAST-FAIL on daily-quota exhaustion ----------
            if _quota_is_daily_exhausted(exc):
                raise RuntimeError(
                    "Your Gemini free-tier daily quota is fully exhausted.\n"
                    "  • Free-tier quota resets at midnight Pacific Time "
                    "(≈12:30 PM IST).\n"
                    "  • Get a new API key at https://aistudio.google.com/apikey\n"
                    "  • Or enable billing at https://console.cloud.google.com/apis\n"
                    "\nOriginal error:\n" + str(exc)
                ) from exc

            if attempt == max_retries:
                logger.error(
                    "Gemini API rate-limit error after %d retries: %s",
                    max_retries,
                    exc,
                )
                raise

            # Exponential backoff with jitter
            delay = base_delay * (2 ** attempt)
            jitter = delay * 0.25 * (2 * random.random() - 1)  # ±25%
            total_delay = delay + jitter

            logger.warning(
                "Gemini API rate-limited (attempt %d/%d). "
                "Retrying in %.1fs …  error=%s",
                attempt + 1,
                max_retries,
                total_delay,
                exc,
            )
            time.sleep(total_delay)

    # Should never reach here, but keep the type-checker happy.
    raise RuntimeError("Unexpected exit from retry loop") from last_exc


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

def resolve_model(
    preferred: str = "gemini-2.5-flash",
    *,
    env_var: str = "GEMINI_MODEL",
) -> str:
    """Return the model name to use.

    Resolution order:
      1. The environment variable ``env_var`` (default ``GEMINI_MODEL``).
      2. ``preferred`` (the code default).

    If the resolved model is NOT in our free-tier catalogue, a warning is
    logged (it's probably a paid-tier model — use at your own cost).
    """
    model = os.getenv(env_var, preferred)

    if model in _FREE_TIER_MODELS:
        rpd, rpm = _FREE_TIER_MODELS[model]
        rpd_max = max(rpd for (r, (rpd, _)) in _FREE_TIER_MODELS.items())
        if rpd < rpd_max:
            logger.info(
                "Model '%s' has a free-tier limit of %d requests/day. "
                "Set GEMINI_MODEL=gemini-2.0-flash for 1500 req/day.",
                model,
                rpd,
            )
    else:
        logger.warning(
            "Model '%s' is not in the known free-tier catalogue. "
            "Assuming paid-tier — monitor your billing.",
            model,
        )

    return model


def model_free_tier_info(model: str) -> dict[str, int] | None:
    """Return {requests_per_day, requests_per_minute} for *model*, or None."""
    info = _FREE_TIER_MODELS.get(model)
    if info is None:
        return None
    return {"requests_per_day": info[0], "requests_per_minute": info[1]}
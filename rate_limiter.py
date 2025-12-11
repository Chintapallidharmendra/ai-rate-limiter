from typing import Dict, Tuple, List, Optional
from threading import Lock
from collections import defaultdict
import time
from dataclasses import dataclass


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    max_requests: int = 100
    window_seconds: int = 3600  # 1 hour


class SlidingWindowEntry:
    """
    Stores request timestamps within a sliding window for user-model pair.

    Uses a list-based implementation for simplicity. In production with higher
    throughput, could use collections.deque or a circular buffer.
    """

    def __init__(self, window_seconds: int):
        self.window_seconds = window_seconds
        self.timestamps: List[float] = []  # Sorted list of request times

    def add_request(self, timestamp: float) -> None:
        """Record a new request at the given timestamp."""
        self.timestamps.append(timestamp)

    def clean_expired(self, window_start: float) -> int:
        """
        Remove requests older than window_start.
        Returns: number of requests removed.
        """
        initial_count = len(self.timestamps)
        # Keep only timestamps >= window_start
        self.timestamps = [ts for ts in self.timestamps if ts >= window_start]
        return initial_count - len(self.timestamps)

    def get_current_count(self, window_start: float) -> int:
        """
        Return count of requests in current window.
        Assumes clean_expired was called first.
        """
        return len(self.timestamps)

    def is_empty(self) -> bool:
        """Check if no requests recorded."""
        return len(self.timestamps) == 0


class RateLimiter:
    """
    Thread-safe, in-memory Sliding Window Log rate limiter for AI inference.

    Example usage:
        limiter = RateLimiter(max_requests=100, window_seconds=3600)

        if limiter.allow("user123", "gpt-4"):
            # Process request
            pass
        else:
            # Rate limit exceeded, return 429
            pass
    """

    def __init__(self, max_requests: int = 100, window_seconds: int = 3600):
        """
        Initialize the rate limiter.

        Args:
            max_requests: Maximum requests allowed per window (default 100)
            window_seconds: Time window in seconds (default 3600 = 1 hour)
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds

        # Storage: key -> SlidingWindowEntry
        self._windows: Dict[str, SlidingWindowEntry] = {}

        self._locks: Dict[str, Lock] = defaultdict(Lock)

        # Global lock for accessing _windows dict structure
        self._dict_lock = Lock()

        # Metrics
        self._allowed_count = 0
        self._denied_count = 0
        self._metrics_lock = Lock()

    def _get_key(self, user_id: str, model_id: str) -> str:
        """Generate cache key for user-model pair."""
        return f"{user_id}:{model_id}"

    def _get_or_create_window(self, key: str) -> SlidingWindowEntry:
        """
        Get or create a SlidingWindowEntry for the given key.

        Must be called while holding either dict_lock or specific key lock.
        """
        if key not in self._windows:
            self._windows[key] = SlidingWindowEntry(self.window_seconds)
        return self._windows[key]

    def allow(self, user_id: str, model_id: str) -> bool:
        """
        Check if a request from user is allowed for the given model.

        Args:
            user_id: Unique identifier for the user/tenant
            model_id: Unique identifier for the model (e.g., "gpt-4")

        Returns:
            True if request is allowed, False if rate limit exceeded
        """
        key = self._get_key(user_id, model_id)
        now = time.time()
        window_start = now - self.window_seconds

        # Acquire per-key lock for thread safety
        with self._locks[key]:
            # Get or create the window entry
            with self._dict_lock:
                window_entry = self._get_or_create_window(key)

            # Clean expired requests (older than window_start)
            window_entry.clean_expired(window_start)

            # Get current request count
            current_count = window_entry.get_current_count(window_start)

            # Check limit and record if allowed
            if current_count < self.max_requests:
                window_entry.add_request(now)

                with self._metrics_lock:
                    self._allowed_count += 1

                return True
            else:
                with self._metrics_lock:
                    self._denied_count += 1

                return False

    def get_request_count(self, user_id: str, model_id: str) -> int:
        """
        Get current request count for a user-model pair.

        Useful for monitoring and debugging.

        Args:
            user_id: Unique identifier for the user/tenant
            model_id: Unique identifier for the model

        Returns:
            Number of requests within the current window
        """
        key = self._get_key(user_id, model_id)
        now = time.time()
        window_start = now - self.window_seconds

        with self._locks[key]:
            with self._dict_lock:
                if key not in self._windows:
                    return 0
                window_entry = self._windows[key]

            window_entry.clean_expired(window_start)
            return window_entry.get_current_count(window_start)

    def reset_user(self, user_id: str, model_id: Optional[str] = None) -> None:
        """
        Reset rate limit for a user (admin operation).

        Args:
            user_id: User to reset
            model_id: If provided, reset only for this model.
                     If None, reset all models for this user.
        """
        if model_id:
            key = self._get_key(user_id, model_id)
            with self._locks[key]:
                with self._dict_lock:
                    if key in self._windows:
                        del self._windows[key]
        else:
            # Reset all models for user
            with self._dict_lock:
                keys_to_remove = [
                    k
                    for k in self._windows.keys()
                    if k.startswith(f"{user_id}:")
                ]
                for key in keys_to_remove:
                    with self._locks[key]:
                        del self._windows[key]

    def get_metrics(self) -> Dict:
        """
        Get rate limiter metrics.

        Returns:
            Dictionary with allowed_count, denied_count, etc.
        """
        with self._metrics_lock:
            total = self._allowed_count + self._denied_count
            deny_rate = (self._denied_count / total * 100) if total > 0 else 0

        return {
            "allowed_count": self._allowed_count,
            "denied_count": self._denied_count,
            "total_requests": self._allowed_count + self._denied_count,
            "deny_rate_percent": deny_rate,
            "active_keys": len(self._windows),
        }

    def reset_metrics(self) -> None:
        """Reset all metrics."""
        with self._metrics_lock:
            self._allowed_count = 0
            self._denied_count = 0


class MultiTierRateLimiter:
    """
    Advanced: Multi-tier rate limiter for AI systems.

    Enforces rate limits at multiple levels:
    1. Per user-model pair (e.g., "user123:gpt-4")
    2. Per model globally (e.g., all users on "gpt-4")
    3. Per model tier (e.g., high-cost models vs low-cost)

    Example:
        limiter = MultiTierRateLimiter(
            per_user_model=RateLimitConfig(100, 3600),
            per_model=RateLimitConfig(10000, 3600),
        )

        if limiter.allow("user123", "gpt-4"):
            process_request()
    """

    def __init__(
        self,
        per_user_model: Optional[RateLimitConfig] = None,
        per_model: Optional[RateLimitConfig] = None,
        per_tier: Optional[Dict[str, RateLimitConfig]] = None,
    ):
        """
        Initialize multi-tier rate limiter.

        Args:
            per_user_model: Limit per (user, model) pair
            per_model: Global limit per model across all users
            per_tier: Limits per model tier 
                (e.g., {"high": config, "low": config})
        """
        self.per_user_model = per_user_model or RateLimitConfig(100, 3600)
        self.per_model = per_model or RateLimitConfig(10000, 3600)
        self.per_tier = per_tier or {}

        self.user_model_limiter = RateLimiter(
            self.per_user_model.max_requests,
            self.per_user_model.window_seconds,
        )
        self.model_limiter = RateLimiter(
            self.per_model.max_requests, self.per_model.window_seconds
        )
        self.tier_limiters: Dict[str, RateLimiter] = {}
        for tier, config in self.per_tier.items():
            self.tier_limiters[tier] = RateLimiter(
                config.max_requests, config.window_seconds
            )

    def get_model_tier(self, model_id: str) -> str:
        """
        Determine the tier of a model.

        Override this method or set via constructor.
        Default: all models in 'standard' tier.
        """
        tier_map = {
            "gpt-4": "high",
            "gpt-4-32k": "high",
            "claude-2": "high",
            "llama-70b": "medium",
            "gpt-3.5-turbo": "medium",
            "llama-7b": "low",
        }
        return tier_map.get(model_id, "standard")

    def allow(self, user_id: str, model_id: str) -> Tuple[bool, str]:
        """
        Check if request is allowed across all tiers.

        Returns:
            (allowed: bool, reason: str)
            If denied, reason explains which tier rejected it.
        """
        # Check per-user-model limit
        if not self.user_model_limiter.allow(user_id, model_id):
            return False, f"User {user_id} exceeded limit for model {model_id}"

        # Check per-model global limit
        if not self.model_limiter.allow("__global__", model_id):
            return False, f"Global limit exceeded for model {model_id}"

        # Check per-tier limit if applicable
        tier = self.get_model_tier(model_id)
        if tier in self.tier_limiters:
            if not self.tier_limiters[tier].allow(
                "__global__", f"tier-{tier}"
            ):
                return False, f"Tier {tier} global limit exceeded"

        return True, "Allowed"

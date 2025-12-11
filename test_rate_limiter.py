"""
Tests for the rate limiter.
"""

import pytest
import time
import threading
from typing import List
from rate_limiter import (
    RateLimiter,
    SlidingWindowEntry,
    RateLimitConfig,
    MultiTierRateLimiter,
)


class TestSlidingWindowEntry:
    """Tests for the SlidingWindowEntry data structure."""

    def test_add_and_count_requests(self):
        """Test adding requests and counting."""
        entry = SlidingWindowEntry(window_seconds=3600)
        now = time.time()

        entry.add_request(now)
        entry.add_request(now + 1)
        entry.add_request(now + 2)

        assert entry.get_current_count(now - 100) == 3

    def test_clean_expired_requests(self):
        """Test that old requests are removed."""
        entry = SlidingWindowEntry(window_seconds=3600)
        base_time = 1000.0

        # Add requests at different times
        entry.add_request(base_time)
        entry.add_request(base_time + 100)
        entry.add_request(base_time + 200)

        # Clean: remove anything before base_time + 150
        removed = entry.clean_expired(base_time + 150)

        assert removed == 2  # Removed first two tests
        assert entry.get_current_count(base_time + 150) == 1

    def test_empty_after_cleanup(self):
        """Test that entry is empty after all requests expire."""
        entry = SlidingWindowEntry(window_seconds=3600)
        entry.add_request(1000.0)
        entry.add_request(1001.0)

        entry.clean_expired(2000.0)  # All expired

        assert entry.is_empty()
        assert entry.get_current_count(2000.0) == 0


class TestBasicRateLimiting:
    """Tests for basic rate limiting functionality."""

    def test_allow_within_limit(self):
        """Test that requests are allowed within the limit."""
        limiter = RateLimiter(max_requests=5, window_seconds=3600)

        for i in range(5):
            assert limiter.allow("user1", "model-a") is True

    def test_deny_after_limit(self):
        """Test that requests are denied after hitting limit."""
        limiter = RateLimiter(max_requests=3, window_seconds=3600)

        # Allow first 3
        for i in range(3):
            assert limiter.allow("user1", "model-a") is True

        # Deny 4th and beyond
        assert limiter.allow("user1", "model-a") is False
        assert limiter.allow("user1", "model-a") is False

    def test_separate_users_independent(self):
        """Test that rate limits are per-user."""
        limiter = RateLimiter(max_requests=2, window_seconds=3600)

        # User 1 hits limit
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is False

        # User 2 should not be affected
        assert limiter.allow("user2", "model-a") is True
        assert limiter.allow("user2", "model-a") is True
        assert limiter.allow("user2", "model-a") is False

    def test_separate_models_independent(self):
        """Test that rate limits are per-model."""
        limiter = RateLimiter(max_requests=2, window_seconds=3600)

        # Model A hits limit
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is False

        # Model B should not be affected
        assert limiter.allow("user1", "model-b") is True
        assert limiter.allow("user1", "model-b") is True
        assert limiter.allow("user1", "model-b") is False

    def test_get_request_count(self):
        """Test getting current request count."""
        limiter = RateLimiter(max_requests=100, window_seconds=3600)

        assert limiter.get_request_count("user1", "model-a") == 0

        limiter.allow("user1", "model-a")
        assert limiter.get_request_count("user1", "model-a") == 1

        limiter.allow("user1", "model-a")
        assert limiter.get_request_count("user1", "model-a") == 2

    def test_metrics_tracking(self):
        """Test that allowed/denied counts are tracked."""
        limiter = RateLimiter(max_requests=2, window_seconds=3600)

        limiter.allow("user1", "model-a")
        limiter.allow("user1", "model-a")
        limiter.allow("user1", "model-a")  # Denied

        metrics = limiter.get_metrics()
        assert metrics["allowed_count"] == 2
        assert metrics["denied_count"] == 1
        assert metrics["total_requests"] == 3
        assert metrics["deny_rate_percent"] == pytest.approx(33.33, rel=1)


class TestSlidingWindowBehavior:
    """Tests for sliding window expiration."""

    def test_window_expiration_allows_new_requests(self):
        """Test that requests are allowed after window expires."""
        limiter = RateLimiter(max_requests=2, window_seconds=1)

        # Fill the window
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is False

        # Wait for window to expire
        time.sleep(1.1)

        # Should be allowed again
        assert limiter.allow("user1", "model-a") is True
        assert limiter.get_request_count("user1", "model-a") == 1

    def test_partial_window_expiration(self):
        """Test that old requests fall off sliding window."""
        limiter = RateLimiter(max_requests=100, window_seconds=2)

        # Add 50 requests
        for _ in range(50):
            limiter.allow("user1", "model-a")

        assert limiter.get_request_count("user1", "model-a") == 50

        # Wait 1 second (middle of window)
        time.sleep(1)

        # Add 30 more
        for _ in range(30):
            limiter.allow("user1", "model-a")

        count_at_middle = limiter.get_request_count("user1", "model-a")
        assert count_at_middle == 80

        # Wait for first batch to expire
        time.sleep(1.1)

        # First 50 should be gone, only 30 remain
        count_after_expiry = limiter.get_request_count("user1", "model-a")
        assert count_after_expiry == 30


class TestConcurrency:
    """Tests for thread safety and concurrent access."""

    def test_concurrent_requests_same_user(self):
        """Test that concurrent requests from same user are handled."""
        limiter = RateLimiter(max_requests=100, window_seconds=3600)
        results: List[bool] = []
        lock = threading.Lock()

        def make_request():
            result = limiter.allow("user1", "model-a")
            with lock:
                results.append(result)

        threads = [threading.Thread(target=make_request) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 100 allowed, rest denied
        assert sum(results) == 100
        assert len(results) == 100

    def test_concurrent_requests_different_users(self):
        """Test concurrent requests from different users."""
        limiter = RateLimiter(max_requests=10, window_seconds=3600)
        results: List[bool] = []
        lock = threading.Lock()

        def make_requests(user_id, count):
            for _ in range(count):
                result = limiter.allow(user_id, "model-a")
                with lock:
                    results.append(result)

        # 10 users, 5 requests each = 50 total
        threads = [
            threading.Thread(target=make_requests, args=(f"user{i}", 5))
            for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Each user allowed 10 requests, denied 0 (each only made 5)
        assert len(results) == 50
        assert sum(results) == 50

    def test_concurrent_burst_single_key(self):
        """Test rapid-fire requests to same key from multiple threads."""
        limiter = RateLimiter(max_requests=50, window_seconds=3600)
        results: List[bool] = []
        lock = threading.Lock()

        def burst_requests():
            for _ in range(10):
                result = limiter.allow("user1", "model-a")
                with lock:
                    results.append(result)

        # 10 threads, each making 10 requests = 100 total
        threads = [threading.Thread(target=burst_requests) for _ in range(10)]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly 50 allowed (first 50 requests), rest denied
        allowed = sum(results)
        assert allowed == 50
        assert len(results) == 100


class TestReset:
    """Tests for reset functionality."""

    def test_reset_single_model(self):
        """Test resetting rate limit for user-model pair."""
        limiter = RateLimiter(max_requests=2, window_seconds=3600)

        limiter.allow("user1", "model-a")
        limiter.allow("user1", "model-a")
        assert limiter.allow("user1", "model-a") is False

        # Reset
        limiter.reset_user("user1", "model-a")

        # Should be allowed again
        assert limiter.allow("user1", "model-a") is True

    def test_reset_all_models_for_user(self):
        """Test resetting all models for a user."""
        limiter = RateLimiter(max_requests=1, window_seconds=3600)

        limiter.allow("user1", "model-a")
        limiter.allow("user1", "model-b")

        assert limiter.allow("user1", "model-a") is False
        assert limiter.allow("user1", "model-b") is False

        # Reset all
        limiter.reset_user("user1")

        # Both should be allowed again
        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-b") is True


class TestMultiTierRateLimiter:
    """Tests for multi-tier rate limiting."""

    def test_user_model_limit_enforced(self):
        """Test that per-user-model limit is enforced."""
        config = RateLimitConfig(max_requests=2, window_seconds=3600)
        limiter = MultiTierRateLimiter(per_user_model=config)

        allowed, reason = limiter.allow("user1", "gpt-4")
        assert allowed is True

        allowed, reason = limiter.allow("user1", "gpt-4")
        assert allowed is True

        allowed, reason = limiter.allow("user1", "gpt-4")
        assert allowed is False
        assert "user1" in reason

    def test_global_model_limit_enforced(self):
        """Test that global per-model limit is enforced."""
        config = RateLimitConfig(max_requests=3, window_seconds=3600)
        limiter = MultiTierRateLimiter(per_model=config)

        # Two users, 2 requests each = 4 total, should hit limit at 3
        allowed, _ = limiter.allow("user1", "gpt-4")
        assert allowed is True

        allowed, _ = limiter.allow("user1", "gpt-4")
        assert allowed is True

        allowed, _ = limiter.allow("user2", "gpt-4")
        assert allowed is True

        # 4th request should be denied
        allowed, reason = limiter.allow("user2", "gpt-4")
        assert allowed is False
        assert "global" in reason.lower()

    def test_model_tier_classification(self):
        """Test that models are classified into tiers."""
        limiter = MultiTierRateLimiter()

        assert limiter.get_model_tier("gpt-4") == "high"
        assert limiter.get_model_tier("gpt-3.5-turbo") == "medium"
        assert limiter.get_model_tier("llama-7b") == "low"
        assert limiter.get_model_tier("unknown-model") == "standard"

    def test_all_tiers_checked(self):
        """Test that request must pass all tier checks."""
        user_config = RateLimitConfig(max_requests=100, window_seconds=3600)
        model_config = RateLimitConfig(max_requests=2, window_seconds=3600)

        limiter = MultiTierRateLimiter(
            per_user_model=user_config, per_model=model_config
        )

        # First two requests allowed (global limit is 2)
        assert limiter.allow("user1", "gpt-4")[0] is True
        assert limiter.allow("user2", "gpt-4")[0] is True

        # Third request denied by global limit
        assert limiter.allow("user3", "gpt-4")[0] is False


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_zero_limit(self):
        """Test behavior with zero request limit."""
        limiter = RateLimiter(max_requests=0, window_seconds=3600)

        assert limiter.allow("user1", "model-a") is False

    def test_very_small_window(self):
        """Test with very small time window."""
        limiter = RateLimiter(max_requests=1, window_seconds=0.01)

        assert limiter.allow("user1", "model-a") is True
        assert limiter.allow("user1", "model-a") is False

        time.sleep(0.02)
        assert limiter.allow("user1", "model-a") is True

    def test_large_number_of_requests(self):
        """Test with large number of requests."""
        limiter = RateLimiter(max_requests=10000, window_seconds=3600)

        # Add 10000 requests
        for i in range(10000):
            assert limiter.allow("user1", "model-a") is True

        # 10001st should be denied
        assert limiter.allow("user1", "model-a") is False

    def test_many_users_and_models(self):
        """Test with many concurrent user-model pairs."""
        limiter = RateLimiter(max_requests=10, window_seconds=3600)

        for user_id in range(100):
            for model_id in range(10):
                for _ in range(10):
                    assert (
                        limiter.allow(f"user{user_id}", f"model{model_id}")
                        is True
                    )

                # 11th should be denied
                assert (
                    limiter.allow(f"user{user_id}", f"model{model_id}")
                    is False
                )

    def test_rapid_sequential_calls(self):
        """Test rapid-fire sequential calls."""
        limiter = RateLimiter(max_requests=100, window_seconds=3600)

        start_time = time.time()
        for i in range(100):
            assert limiter.allow("user1", "model-a") is True
        elapsed = time.time() - start_time

        # Should be very fast (sub-second)
        assert elapsed < 1.0

        assert limiter.allow("user1", "model-a") is False


class TestMetrics:
    """Tests for metrics collection."""

    def test_metrics_calculation(self):
        """Test that metrics are calculated correctly."""
        limiter = RateLimiter(max_requests=5, window_seconds=3600)

        for _ in range(5):
            limiter.allow("user1", "model-a")
        for _ in range(3):
            limiter.allow("user1", "model-a")  # All denied

        metrics = limiter.get_metrics()
        assert metrics["allowed_count"] == 5
        assert metrics["denied_count"] == 3
        assert metrics["total_requests"] == 8
        assert metrics["deny_rate_percent"] == pytest.approx(37.5, rel=0.1)

    def test_metrics_reset(self):
        """Test resetting metrics."""
        limiter = RateLimiter(max_requests=5, window_seconds=3600)

        for _ in range(5):
            limiter.allow("user1", "model-a")

        metrics1 = limiter.get_metrics()
        assert metrics1["allowed_count"] == 5

        limiter.reset_metrics()
        metrics2 = limiter.get_metrics()
        assert metrics2["allowed_count"] == 0
        assert metrics2["denied_count"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

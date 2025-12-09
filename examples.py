"""
Practical Usage Examples and Integration Guide

Demonstrates how to use the rate limiter in real scenarios:
- Single-node API server
- FastAPI integration
- Kubernetes sidecar pattern
- Monitoring and observability
"""

import uuid
import time
from enum import Enum
from typing import Dict
from threading import Lock
from rate_limiter import RateLimiter
from fastapi import FastAPI, HTTPException, Header

# ============================================================================
# EXAMPLE 1: BASIC SINGLE-NODE USAGE
# ============================================================================


def example_basic_usage():
    """Simple usage of the in-memory rate limiter."""

    # Create rate limiter: 100 requests per hour
    limiter = RateLimiter(max_requests=100, window_seconds=3600)

    # Check if request is allowed
    user_id = "user123"
    model_id = "gpt-4"

    if limiter.allow(user_id, model_id):
        print("✓ Request allowed")
        # Process the inference request
    else:
        print("✗ Rate limit exceeded, return 429")
        # Return HTTP 429 Too Many Requests


# ============================================================================
# EXAMPLE 2: FASTAPI INTEGRATION
# ============================================================================


def example_fastapi_integration():
    """Integrate rate limiter with FastAPI for AI model serving."""

    app = FastAPI()
    # Initialize rate limiter
    # Different limits per model
    limiters = {
        "gpt-4": RateLimiter(max_requests=100, window_seconds=3600),
        "gpt-3.5-turbo": RateLimiter(max_requests=500, window_seconds=3600),
        "llama-70b": RateLimiter(max_requests=300, window_seconds=3600),
    }

    @app.post("/inference")
    async def inference(
        request_data: dict,
        x_user_id: str = Header(...),
        x_model_id: str = Header(...),
    ):
        """Handle inference requests with rate limiting."""

        # Validate model
        if x_model_id not in limiters:
            raise HTTPException(status_code=400, detail="Unknown model")

        limiter = limiters[x_model_id]

        # Check rate limit
        if not limiter.allow(x_user_id, x_model_id):
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        # Process inference
        # ... call to model router/GPU pool ...

        return {
            "request_id": str(uuid.uuid4()),
            "model": x_model_id,
            "status": "processing",
        }

    @app.get("/rate-limit/status")
    async def status(x_user_id: str = Header(...)):
        """Check current rate limit status for user."""
        status_dict = {}

        for model_id, limiter in limiters.items():
            count = limiter.get_request_count(x_user_id, model_id)
            max_req = limiter.max_requests
            status_dict[model_id] = {
                "requests_used": count,
                "requests_limit": max_req,
                "requests_remaining": max_req - count,
            }

        return status_dict


# ============================================================================
# EXAMPLE 3: MULTI-TIER RATE LIMITING FOR DIFFERENT USER TYPES
# ============================================================================


def example_multi_tier_usage():
    """Rate limit different user types differently."""

    class UserTier(Enum):
        INTERNAL = "internal"  # 500 req/hour
        PREMIUM = "premium"  # 200 req/hour
        STANDARD = "standard"  # 100 req/hour
        FREE = "free"  # 10 req/hour

    limiters = {
        UserTier.INTERNAL: RateLimiter(500, 3600),
        UserTier.PREMIUM: RateLimiter(200, 3600),
        UserTier.STANDARD: RateLimiter(100, 3600),
        UserTier.FREE: RateLimiter(10, 3600),
    }

    def check_rate_limit(user_id: str, user_tier: UserTier, model_id: str):
        limiter = limiters[user_tier]
        return limiter.allow(user_id, model_id)

    # Examples
    print(check_rate_limit("internal-team-1", UserTier.INTERNAL, "gpt-4"))
    print(check_rate_limit("customer-123", UserTier.PREMIUM, "gpt-4"))
    print(check_rate_limit("api-user-456", UserTier.FREE, "gpt-4"))


# ============================================================================
# EXAMPLE 4: TOKEN-BASED RATE LIMITING
# ============================================================================


def example_token_based_limiting():
    """Rate limit based on token cost instead of request count."""

    class TokenBucketLimiter:
        """Rate limiter based on token cost."""

        def __init__(self, max_tokens: int, refill_seconds: int = 3600):
            self.max_tokens = max_tokens
            self.refill_seconds = refill_seconds
            self.user_tokens: Dict[str, tuple] = (
                {}
            )  # (tokens, last_refill_time)
            self.lock = Lock()

        def get_token_cost(self, input_tokens: int, output_tokens: int) -> int:
            """Calculate token cost for a request."""
            # Cost = 1x input + 2x output (common pricing model)
            return input_tokens + output_tokens * 2

        def allow(
            self,
            user_id: str,
            model_id: str,
            input_tokens: int,
            output_tokens: int,
        ) -> bool:
            """Check if user can afford this request."""

            key = f"{user_id}:{model_id}"
            cost = self.get_token_cost(input_tokens, output_tokens)
            now = time.time()

            with self.lock:
                if key not in self.user_tokens:
                    # New user, start with max tokens
                    self.user_tokens[key] = (self.max_tokens, now)

                tokens, last_refill = self.user_tokens[key]

                # Refill tokens based on elapsed time
                elapsed = now - last_refill
                refill_rate = self.max_tokens / self.refill_seconds
                refilled_tokens = min(
                    self.max_tokens, tokens + refill_rate * elapsed
                )

                # Check if we can afford this request
                if refilled_tokens >= cost:
                    # Deduct cost
                    self.user_tokens[key] = (refilled_tokens - cost, now)
                    return True
                else:
                    return False

    # Usage
    limiter = TokenBucketLimiter(max_tokens=10000, refill_seconds=3600)

    # Request with 500 input + 1000 output tokens
    # Cost = 500 + 1000*2 = 2500 tokens
    allowed = limiter.allow(
        user_id="user123",
        model_id="gpt-4",
        input_tokens=500,
        output_tokens=1000,
    )
    print(f"Token-based rate limit check: {allowed}")

# ============================================================================
# MAIN: RUNNABLE EXAMPLES
# ============================================================================


if __name__ == "__main__":

    print("=" * 70)
    print("AI INFERENCE RATE LIMITER - PRACTICAL EXAMPLES")
    print("=" * 70)

    print("\n[Example 1: Basic Usage]")
    example_basic_usage()

    print("\n[Example 3: Multi-Tier Rate Limiting]")
    example_multi_tier_usage()

    print("\n[FastAPI Integration Code]")
    print("See example_fastapi_integration() for FastAPI app")

    print("\n[Token-Based Rate Limiting]")
    example_token_based_limiting()

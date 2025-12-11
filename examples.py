import uuid
from enum import Enum
from rate_limiter import RateLimiter
from fastapi import FastAPI, HTTPException, Header


def example_basic_usage():
    """Simple usage of the in-memory rate limiter."""
    limiter = RateLimiter(max_requests=100, window_seconds=3600)
    user_id = "user123"
    model_id = "gpt-4"
    if limiter.allow(user_id, model_id):
        print("✓ Request allowed")
    else:
        print("✗ Rate limit exceeded, return 429")


def example_fastapi_integration():
    """Integrate rate limiter with FastAPI for AI model serving.
    This is only shown here. It does not run as part of examples.py
    FastAPI server should be run separately.
    """

    app = FastAPI()
    limiters = {
        "gpt-4": RateLimiter(max_requests=100, window_seconds=3600),
        "gpt-3.5-turbo": RateLimiter(max_requests=500, window_seconds=3600),
        "llama-70b": RateLimiter(max_requests=300, window_seconds=3600),
    }

    @app.post("/inference")
    async def inference(
        request_data: dict,  # Payload for inference request (not used here)
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


if __name__ == "__main__":

    print("\n[Example 1: Basic Usage]")
    example_basic_usage()

    print("\n[Example 2: FastAPI Integration Code]")
    print("See example_fastapi_integration() for FastAPI app")

    print("\n[Example 3: Multi-Tier Rate Limiting]")
    example_multi_tier_usage()

# Integration & Deployment Guide

Complete guide for integrating the AI Inference Rate Limiter into production systems.

## Table of Contents

1. [Quick Integration](#quick-integration)
2. [API Server Integration](#api-server-integration)
3. [Kubernetes Deployment](#kubernetes-deployment)
4. [Monitoring & Observability](#monitoring--observability)
5. [Troubleshooting](#troubleshooting)

---

## Quick Integration

### Step 1: Import and Initialize

```python
from rate_limiter import RateLimiter

# Initialize with your rate limit settings
limiter = RateLimiter(
    max_requests=100,      # 100 requests
    window_seconds=3600    # per hour
)
```

### Step 2: Add Rate Limit Check

```python
# In your request handler
user_id = request.headers.get('X-User-ID')
model_id = request.headers.get('X-Model-ID')

if not limiter.allow(user_id, model_id):
    return Response(
        status_code=429,
        content="Rate limit exceeded",
        headers={"Retry-After": "3600"}
    )

# Process request
return process_inference(user_id, model_id)
```

---

## API Server Integration

### FastAPI Example

```python
from fastapi import FastAPI, HTTPException, Header, status
from rate_limiter import RateLimiter
import os

app = FastAPI()

# Initialize rate limiters for different models
limiters = {
    "gpt-4": RateLimiter(max_requests=100, window_seconds=3600),
    "gpt-3.5-turbo": RateLimiter(max_requests=500, window_seconds=3600),
    "llama-70b": RateLimiter(max_requests=300, window_seconds=3600),
}

@app.post("/v1/inference")
async def inference(
    request_body: dict,
    x_user_id: str = Header(..., description="User ID"),
    x_model_id: str = Header(..., description="Model ID")
):
    """AI inference endpoint with rate limiting."""

    # Validate model
    if x_model_id not in limiters:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown model: {x_model_id}"
        )

    # Check rate limit
    limiter = limiters[x_model_id]
    if not limiter.allow(x_user_id, x_model_id):
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded",
            headers={"Retry-After": "3600"}
        )

    # Process inference
    try:
        result = await call_model_service(
            x_user_id,
            x_model_id,
            request_body
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/v1/rate-limit/status")
async def rate_limit_status(x_user_id: str = Header(...)):
    """Check current rate limit status for user."""
    status_response = {}

    for model_id, limiter in limiters.items():
        count = limiter.get_request_count(x_user_id, model_id)
        max_req = limiter.max_requests

        status_response[model_id] = {
            "requests_used": count,
            "requests_limit": max_req,
            "requests_remaining": max_req - count,
            "reset_in_seconds": 3600 - (
                count > 0 and 0 or 3600  # Simplified
            )
        }

    return status_response


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    metrics = {
        model_id: limiter.get_metrics()
        for model_id, limiter in limiters.items()
    }
    return {"status": "healthy", "metrics": metrics}
```
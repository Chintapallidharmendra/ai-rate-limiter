# AI Rate Limiter

A simple rate limiter for AI systems. It keeps
track of how many requests each person makes and stops them when they go over their limit.

## What's in Here?

- **`rate_limiter.py`** - The main code (easy to read and understand)
- **`test_rate_limiter.py`** - tests to make sure it works correctly
- **`distributed_rate_limiter.py`** - For bigger systems using Redis & LUA script
- **`DESIGN.md`** - How the algorithm works (explained simply)

## Quick Start

```bash
pip install -r requirements.txt
```

### Using It

```python
from rate_limiter import RateLimiter

# Create a rate limiter: 100 requests per hour
limiter = RateLimiter(max_requests=100, window_seconds=3600)

# Check if someone can make a request
if limiter.allow("dharmendra", "gpt-4"):
    print("Sure, go ahead!")
else:
    print("Sorry, you've used up your quota")

# See how many requests someone has made
count = limiter.get_request_count("dharmendra", "gpt-4")
print(f"Dharmendra has used {count} out of 100 requests")
```

## How It Works

When someone makes a request:

1. Check: how many requests did they make in the last hour?
2. Under 100? Let it through
3. At or over 100? Block it

Does this with **Sliding Window Log** - essentially remembers when each request
hit, and automatically forgets about anything older than an hour. Simple but
effective.

## Testing

Run this command to execute the tests:

```bash
pytest test_rate_limiter.py -v
```

This runs tests that check:

- Basic allow/deny behavior
- Multiple users don't interfere with each other
- The time window actually expires correctly
- It works when many people request at the same time
- Weird edge cases (like zero limit, or very short time windows)

## For Different User Types

You can set different limits for different types of users:

```python
from rate_limiter import MultiTierRateLimiter, RateLimitConfig

limiter = MultiTierRateLimiter(
    per_user_model=RateLimitConfig(100, 3600),   # Normal users
    per_model=RateLimitConfig(10000, 3600),       # All users on one model
)

allowed, reason = limiter.allow("dharmendra", "gpt-4")
if not allowed:
    print(f"Denied: {reason}")
```

## Bigger Systems

If you're running a large system with multiple servers, you can use the Redis version:

```python
from distributed_rate_limiter import RedisRateLimiter
import redis

client = redis.Redis(host='localhost', port=6379)
limiter = RedisRateLimiter(client)

if limiter.allow("dharmendra", "gpt-4"):
    # Process the request
    pass
```

## Documentation

- **DESIGN.md** - Detailed explanation of the algorithm and basic system design
- **ARCHITECTURE.md** - Detailed explanation of the project architecture
- **distributed_rate_limiter.py** - How to use it with Redis for bigger systems

## Real Examples

Check out `examples.py` for:

- Basic Usage
- FastAPI integration (use it in your FastAPI app)
- Multi Tier rate limiting

## Why This Matters

Rate limiting protects your AI services by:

- Preventing one person from using up all your GPU time
- Making sure everyone gets a fair share
- Giving you control over costs
- Protecting against abuse

## Questions?

- **How does the algorithm work?** → Read DESIGN.md
- **Does it actually work?** → Run the tests: `pytest test_rate_limiter.py -v`
- **How do I scale it to multiple servers?** → See distributed_rate_limiter.py

---

That's it! It's designed to be simple, straightforward, and actually useful. For an queries or suggestions. Please reach out to [Dharmendra](mailto:chintapallidharmendra@gmail.com).

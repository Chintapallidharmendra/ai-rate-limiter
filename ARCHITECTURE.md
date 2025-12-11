# Project Structure & How It Works

Kept the architecture simple on purpose. We can dicuss and optimize it more if required

## What's in the Box

```
Rate Limiter Project
├── rate_limiter.py (Main File for simple execution)
├── distributed_rate_limiter.py (For multiple servers)
├── test_rate_limiter.py (Tests for rate_limiter functions)
├── examples.py (Added some examples to for sample implementations)
├── DESIGN.md (Explains how it works)
├── INTEGRATION.md (How to use it)
└── README.md (Quick start)
```

## How It Works

### The Basic Idea

1. Keep track of when requests happen
2. When a new request comes, throw away old ones outside your time window
3. Count how many are left
4. If under the limit, allow it; otherwise, block it

### Example: 100 requests per hour

```
Time 0:00 - First hour window ----- Time 1:00
[req][req][req]...[req]
0    5    10        55 min

Count = 100? BLOCKED
Count < 100? ALLOWED


Time 1:01 - Second hour window ----- Time 2:01
                 [req][req]...[req]
                 5    10        60 min

First request dropped off, so now count < 100? ALLOWED
```

## Single Server vs Multiple Servers

### One Server (Simple)

```
Your Code (The api/function you wanted to add apply this ratelimiter on)
    ↓
  RateLimiter (in memory)
    ↓
  Check & Block/Allow
```

Fast, no dependencies, works great for small to medium.

### Multiple Servers (Advanced)

```
Server 1          Server 2          Server 3
  ↓                ↓                  ↓
RateLimiter    RateLimiter     RateLimiter
  ↓                ↓                  ↓
  └─────────────── Redis ────────────┘
               (Shared Memory)
```

All servers share the same limits via Redis.

## Data Structures

### In Memory (Single Server)

```python
{
    "user123:gpt-4": [
        1702015200,  # timestamp of request 1
        1702015205,  # timestamp of request 2
        ...          # more timestamps
    ]
}
```

Simple list of timestamps. Remove old ones, count the rest.

### Redis (Multiple Servers)

Same idea, but stored in Redis using a "Sorted Set":

```
Key: "ratelimit:user123:gpt-4"
Values: [1702015200, 1702015205, ...]
```

Redis handles removing old ones automatically.

## The Request Flow

```
Request comes in
    ↓
Extract: which user? which model?
    ↓
Ask limiter: allow(user_id, model_id)?
    ↓
    ├─ Clean old requests (older than 1 hour)
    ├─ Count remaining
    ├─ Is count < 100?
    │
    ├─ YES → Record this request, return TRUE
    │
    └─ NO → Return FALSE
    ↓
Handler
    ├─ if TRUE: Process the request
    └─ if FALSE: Return error 429 "Rate limited"
```

## Thread Safety (Multiple Requests at Same Time)

When requests come at the same time:

```
Request 1 ──┐
Request 2 ──┤─→ Mutex Lock ─→ One at a time ─→ Release
Request 3 ──┘
```

Each user+model pair has its own lock. Safe!

## Tests Included

- Basic blocking works
- Users are isolated (Alice's limit doesn't affect Bob)
- Requests expire correctly
- Safe under 100+ threads
- Edge cases (zero limit, tiny window, etc.)
- Multi-tier (different limits for free/paid users)

Run all with: `pytest test_rate_limiter.py -v`

## Code Layout

**rate_limiter.py:**

- `RateLimiter` - Main class
- `SlidingWindowEntry` - Data holder
- `MultiTierRateLimiter` - For free/premium tiers

**distributed_rate_limiter.py:**

- `RedisRateLimiter` - Uses Redis
- Lua script - Makes operations atomic (no race conditions)

**examples.py:**

- FastAPI example
- Flask example
- How to use with Redis
- Multi-tier example
- Cost-aware limiting

**test_rate_limiter.py:**

- Test cases
- Concurrency tests
- Edge case tests
- Multi-tier tests

## Where Each File Belongs

**Learning:**

1. Start with DESIGN.md (explains the why)
2. Read rate_limiter.py (explains the how)
3. Look at examples.py (shows real usage)

**Using:**

1. Read INTEGRATION.md (your framework)
2. Copy code from examples.py
3. Configure limits for your models

**Verifying:**

1. Run tests: `pytest test_rate_limiter.py`
2. Try examples: `python examples.py`

---

That's it! Simple, fast, and works. Please feel free to contact [Dharmendra](mailto:chintapallidharmendra@gmail.com)

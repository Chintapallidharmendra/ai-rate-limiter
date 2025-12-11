# How the Rate Limiter Works

Explanation of how this was designed. Tried to keep it practical, not overly
theoretical.

## The Problem

Running an AI service with unlimited requests = disaster. One user or bot will
consume all your resources and everyone else gets nothing. Happened to me once,
not fun.

Solution: each user gets a quota (say 100 requests/hour). Track how many
they've used and block them if they exceed it.

## The Algorithm: Sliding Window Log

Tried a few approaches (token bucket, fixed window) but Sliding Window Log
works best for this.

Basic idea: Remember when each request happened, throw away anything older than
1 hour, count what's left.

### Example

```
Time: 1:00 PM
Request comes in from Dharmendra
  → Does he have less than 100 requests in the last hour?
  → YES: Let him through, remember this timestamp
  → NO: Tell him to wait

Time: 1:05 PM
Another request from Dharmendra
  → How many requests has he made since 12:05 PM (1 hour ago)?
  → If it's less than 100: OK
  → If it's 100 or more: Sorry, try again later
```

The key insight: the "1 hour window" moves forward in time. It's always "the last 60 minutes" not "until midnight."

## How We Store This

### Simple Version (In One Computer)

```python
# For each person, we keep a list of request times
{
  "dharmendra:gpt-4": [1.23pm, 1.25pm, 1.26pm, ...],
  "pradeep:gpt-4": [1.20pm, 1.30pm, ...],
}

# When a new request comes in:
# 1. Look at the list
# 2. Remove any times older than 1 hour
# 3. Count what's left
# 4. If count < 100, add the new time
```

### Big Version (Multiple Servers)

When you have multiple servers, you can't store lists on individual machines. Instead:

- Use **Redis** (a shared database)
- Store as a "sorted set" by time
- Each entry expires automatically after 1 hour

## Making It Safe with Multiple Requests

If two requests come in at the exact same time from the same person, we need to make sure we don't accidentally let both through if they'd exceed the limit.

**Solution**: Use a special Redis command (Lua script) that:

1. Checks the count
2. Decides allow/deny
3. Records the decision
   ...all in one atomic operation. No race conditions.

```lua
-- All of this happens together, nobody can interrupt
count = redis.count(...)
if count < 100 then
    redis.add(...)  -- Record this request
    return true
else
    return false
end
```

## Per-User, Per-Model Quotas

Maybe Dharmendra gets 100 requests/hour for GPT-4, but 500 requests/hour for small embeddings models (which are cheaper).

We just use different keys:

```python
limiter.allow("dharmendra", "gpt-4")          # Check alice:gpt-4
limiter.allow("dharmendra", "embedding-small")  # Check alice:embedding-small
```

Each one has its own counter.

## Multi-Level Limits

You can enforce multiple limits at once:

1. Per-person-per-model: "Alice, max 100/hour on GPT-4"
2. Per-model global: "Everyone combined, max 10,000/hour on GPT-4"
3. By user type: "Premium users get 500/hour, free users get 10/hour"

We just check all of them. If any says no, the answer is no.

## Why This Matters

Without rate limiting:

- One person could use all your GPU time
- Costs could spiral out of control
- Service gets slow for everyone

With rate limiting:

- Fair access for everyone
- Predictable costs
- Keeps the service responsive

## The Code

The implementation is straightforward:

1. **RateLimiter** class stores request timestamps per user/model
2. **allow()** method checks and updates
3. **get_request_count()** shows how many requests someone has used
4. **get_metrics()** tracks total allowed/denied for monitoring

All the code is in `rate_limiter.py` and it's designed to be readable.

## For Bigger Systems

If you're running across multiple servers:

1. Use the **distributed_rate_limiter.py** version
2. It uses Redis instead of local memory
3. All servers share the same quota counters
4. Uses Lua scripts to keep everything consistent

See `distributed_rate_limiter.py` for details.

---

That's the essence of it. It's simple, reliable, and actually useful.

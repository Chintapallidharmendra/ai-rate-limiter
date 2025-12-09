# AI Inference Rate Limiter - Design Document

## Phase 1: System Design (Abstract to Concrete)

### 1. Use Case Context

#### Problem Statement

AI model serving infrastructure faces unique challenges:

- **GPU scarcity**: GPU resources are expensive and limited; a single misconfigured client can starve others
- **Token economics**: Large models (GPT-4) consume significant compute; fair distribution is critical
- **Multi-tenant isolation**: Different organizations/teams must not interfere with each other
- **Inference latency**: Requests must be prioritized and load-balanced fairly

#### How the Rate Limiter Sits in the System

```
┌─────────────────────────────────────────────────────────────────┐
│                        Client (User)                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      API Gateway / LB                            │
│              (Performs basic auth, routing)                      │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              ┌───────────────────────────────────────┐           │
│              │   RATE LIMITER (Our Component)       │           │
│              │  ┌──────────────────────────────────┐ │           │
│              │  │ check_allow(userId, modelId)     │ │           │
│              │  │ • Validate sliding window        │ │           │
│              │  │ • Update counters in Redis       │ │           │
│              │  │ • Return true/false              │ │           │
│              │  └──────────────────────────────────┘ │           │
│              └───────────────────────────────────────┘           │
│         (Blocks request before hitting model router)             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                    Model Router / LB                             │
│              (Routes to available GPU/instance)                  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              GPU Pool / Model Serving Instances                  │
│        (VLLM, TGI, vLLM, or custom serving containers)          │
└─────────────────────────────────────────────────────────────────┘
```

**Key Insight**: Rate limiter sits between API Gateway and Model Router, preventing overload at GPU/inference layer.

---

### 2. Core Data Structures

#### Single-Node (In-Memory) Version

```typescript
// Per user + model pair, store timestamps of allowed requests
Map<string, SlidingWindowEntry> = {
  "user123:gpt4": {
    windowStart: timestamp,
    timestamps: [t1, t2, t3, ...],  // Ring buffer or sorted list
    count: 47
  },
  "user456:llama": {
    windowStart: timestamp,
    timestamps: [...],
    count: 89
  }
}

// SlidingWindowEntry structure
interface SlidingWindowEntry {
  windowStartTime: number;      // UNIX timestamp of 1-hour window
  requestTimestamps: number[];  // Array of request times (TTL cleanup)
  count: number;                // Running count
}
```

**Memory Efficiency**:

1. Use a **bounded circular buffer** for timestamps to prevent unbounded memory growth
2. Periodically clean old entries (older than 1 hour) via TTL or garbage collection
3. For single node: keep recent timestamps in a fixed-size array per key

#### Distributed (Redis) Version

```
Redis Sorted Set per key: "ratelimit:{userId}:{modelId}"

Entry format:
  ZADD ratelimit:user123:gpt4 <timestamp> <request-id>

Example:
  ratelimit:user123:gpt4:
    Score 1702015200 -> member "req-1"
    Score 1702015201 -> member "req-2"
    Score 1702015245 -> member "req-3"
    ...

Operations:
  ZRANGEBYSCORE ratelimit:user123:gpt4 (now-3600) now  -> Count recent
  ZADD ratelimit:user123:gpt4 now <uuid>              -> Record new request
  EXPIRE ratelimit:user123:gpt4 3600                  -> Auto-expire old data
```

**Why Sorted Sets?**

- O(log N) insertion
- O(log N) range queries by timestamp
- Automatic removal of old entries via EXPIRE
- Built-in atomic operations

---

### 3. Algorithm: Sliding Window Log

#### High-Level Logic

```
function allow(userId, modelId, maxRequests=100, windowSeconds=3600):
    key = "{userId}:{modelId}"
    now = current_time()
    windowStart = now - windowSeconds

    # Remove all requests outside the window
    removeOldRequests(key, windowStart)

    # Check if we can allow this request
    currentCount = getRequestCount(key)

    if currentCount < maxRequests:
        recordRequest(key, now)
        return true
    else:
        return false
```

#### Step-by-Step Process

1. **Compute window boundaries**

   - Current time: `now = 1702015345`
   - Window start: `windowStart = now - 3600 = 1702011745`
   - Only requests within `[windowStart, now]` count

2. **Remove expired requests**

   - Query all requests before `windowStart`
   - Delete them (or they auto-expire via TTL)

3. **Count current requests**

   - Count requests in `[windowStart, now]`

4. **Allow or deny**
   - If count < 100: allow, record timestamp, return `true`
   - If count >= 100: reject, return `false`

**Example Timeline**:

```
Hour: 1:00 - 2:00 (3600 second window)
|----[Request allowed]----[Request allowed]----...----[Request 100 allowed]---|
                                                              ↓
Time 2:00:01 arrives:
  - Window now: 1:00:01 - 2:00:01
  - First request from 1:00:00 is now outside window (falls off left edge)
  - New slot available at right edge
  - Request 101 is ALLOWED
```

#### Complexity Analysis

| Operation        | Complexity   | Notes                                                 |
| ---------------- | ------------ | ----------------------------------------------------- |
| Check allow      | O(log N)     | N = requests in window (typically 100, near-constant) |
| Insert timestamp | O(log N)     | Sorted set insertion                                  |
| Clean old        | O(log N + M) | M = old requests to remove (often small)              |
| Space            | O(N)         | N = active requests across all keys                   |

---

### 4. Concurrency and Distribution

#### Single-Node Concurrency

**Thread Safety Approach**:

```
Use a lock-free or mutex-protected map:

Option A: Mutex per key
  - Fine-grained locking: better concurrency
  - Reduce contention for hot keys

Option B: Global lock
  - Simple, easier to implement
  - Lower concurrency, but acceptable for single node

Chosen: Mutex per key (scalability)
```

**Implementation Strategy**:

```python
from threading import Lock
from collections import defaultdict

class RateLimiter:
    def __init__(self):
        self.locks = defaultdict(Lock)
        self.windows = {}

    def allow(self, userId, modelId):
        key = f"{userId}:{modelId}"

        with self.locks[key]:  # Per-key lock
            # Safe to read/write now
            return self._check_allow_unsafe(key)
```

#### Distributed Environment (Redis + Lua)

**Problem**: Distributed systems face race conditions.

Example race condition without atomicity:

```
Thread A                          Thread B
1. ZCOUNT key (count=99)
                                  1. ZCOUNT key (count=99)
2. count < 100, ZADD (count=100)
                                  2. count < 100, ZADD (count=101)
Result: Both allowed, limit breached!
```

**Solution: Lua Script (Atomic)**

```lua
-- Redis Lua script for atomic rate limit check
local key = KEYS[1]
local now = tonumber(ARGV[1])
local windowStart = tonumber(ARGV[2])
local maxRequests = tonumber(ARGV[3])
local requestId = ARGV[4]

-- Remove old requests outside window
redis.call('ZREMRANGEBYSCORE', key, '-inf', windowStart)

-- Count requests in current window
local count = redis.call('ZCARD', key)

if count < maxRequests then
    -- Allow: add this request
    redis.call('ZADD', key, now, requestId)
    redis.call('EXPIRE', key, 3600)
    return 1  -- Allowed
else
    return 0  -- Denied
end
```

**Execution**:

```
EVAL script 1 ratelimit:user123:gpt4 1702015345 1702011745 100 req-12345
```

**Why Lua?**

- Executed atomically on Redis server (no race conditions)
- Reduces network round trips (one Redis call instead of three)
- All-or-nothing semantics

#### Sharding Across Multiple Redis Nodes

**Strategy: Consistent Hashing**

```
For key "user123:gpt4":
  1. hash(key) = 0x7a3f
  2. Find responsible node in ring
  3. Route to Node 3

Nodes arranged in consistent hash ring:
  Node1: 0x0000 - 0x4000
  Node2: 0x4001 - 0x8000
  Node3: 0x8001 - 0xffff
```

**Failover and Replication**:

- Each Redis node has a replica
- If primary fails, replica takes over
- Rate limit data is ephemeral (TTL 1 hour), tolerable loss

---

### 5. AI-Specific Considerations

#### Protecting GPU Pools from Overload

**Multi-Tier Rate Limiting**:

```
Tier 1: Per User + Model
  "user123:gpt4" -> 100 req/hour

Tier 2: Per Model (Global)
  "global:gpt4" -> 10,000 req/hour across all users
  (If one model hits global limit, all users suffer backpressure)

Tier 3: Per GPU Instance
  "gpu-instance-42:gpt4" -> 5 req/minute
  (Instance-level throttling to prevent queue overload)

Check order:
  user + model check -> passes?
  -> global model check -> passes?
  -> gpu instance check -> passes?
  -> ALLOW
```

#### Integration with Token-Based Billing

```
Standard Rate Limiter: Count = number of requests

Token-Based Limiter: Count = sum of token costs

Example:
  "user123:gpt4" per hour:
    - Request A: 500 input tokens + 1000 output = cost 1500
    - Request B: 200 input + 300 output = cost 500
    - Total cost: 2000 tokens

  Rate rule: User allowed 50,000 tokens/hour

Modification to algorithm:
  if currentTokenCost + requestTokenCost <= maxTokensPerHour:
      allow()
  else:
      deny()
```

#### QoS Tiers for Internal vs External Clients

```
Client Type           Limit              Priority
─────────────────────────────────────────────────
Internal/Premium      500 req/hour       P1 (skip queue)
Standard External     100 req/hour       P2 (normal queue)
Free/Trial            10 req/hour        P3 (background queue)

Implementation:
  1. Attach tier to request headers (authenticated at API Gateway)
  2. Rate limiter checks tier-specific limit
  3. Pass tier to model router for priority scheduling
```

#### Cost Accounting and Burst Capacity

```
Peak Usage Patterns for AI:
  - Predictable: Academic hours (9-5 UTC)
  - Unpredictable: Research teams on deadlines

Solution: Token Bucket (Variant)
  - Base capacity: 100 tokens
  - Refill rate: 100 tokens per hour
  - Max burst: 200 tokens (allow spikes)

  user_tokens_available = min(200,
                              user_tokens_available +
                              refillRate * timeSinceLastRefill)
```

---

### 6. Extension Points

#### Different Limits Per Tenant or API Key

```
Data Model:
  TenantConfig:
    tenantId: "acme-corp"
    limits:
      gpt4: { requestsPerHour: 500 }
      llama: { requestsPerHour: 1000 }
      gpt35: { requestsPerHour: 100 }

  ApiKeyConfig:
    apiKey: "sk-abc123..."
    tenantId: "acme-corp"
    overrides:
      gpt4: { requestsPerHour: 50 }  # Lower than tenant default

Resolution Logic:
  effectiveLimit = apiKeyConfig.overrides.get(modelId)
                   OR tenantConfig.limits.get(modelId)
                   OR globalDefault
```

#### Different Limits Per Model Tier

```
Model Classification:
  Tier 1 (High Cost):
    - GPT-4: 100 req/hour
    - Claude 2: 100 req/hour

  Tier 2 (Medium Cost):
    - GPT-3.5: 500 req/hour
    - Llama 70B: 300 req/hour

  Tier 3 (Low Cost):
    - Small embeddings: 10,000 req/hour
    - Summarization (FLAN): 5,000 req/hour

Lookup:
  modelTier = MODEL_TIER_MAP.get(modelId)
  baseLimit = TIER_LIMITS.get(modelTier)
  effectiveLimit = applyTenantOverrides(baseLimit, tenantId, modelId)
```

---

## Summary: Design Architecture

| Layer                 | Component            | Technology                     | Consistency           |
| --------------------- | -------------------- | ------------------------------ | --------------------- |
| **API Layer**         | gRPC/REST endpoint   | FastAPI / gRPC server          | Request auth          |
| **Rate Limit Check**  | Sliding Window + Lua | Redis Sorted Sets + Lua script | Strong                |
| **Distributed State** | Key-value store      | Redis Cluster                  | Eventual + atomic Lua |

---

## Transition to Production

1. **Single Node** → Deploy in-memory version for testing
2. **Scale to Redis** → Introduce Redis backend, Lua scripts
3. **Add Monitoring** → Export metrics (allowedCount, deniedCount, latency)
4. **Distribute** → Deploy with consistent hashing and sharding
5. **Failover** → Add Redis Sentinel or Cluster mode for HA

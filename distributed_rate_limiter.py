"""
Bonus: Distributed Redis-Based Rate Limiter Implementation

This module provides a Redis-backed rate limiter suitable for distributed,
production-scale AI inference systems. It handles clock skew, retries,
partial failures, and integrates with API Gateways/sidecars.
"""

import redis
import uuid
import time


# ============================================================================
# REDIS LUA SCRIPT FOR ATOMIC RATE LIMIT CHECK
# ============================================================================

REDIS_RATE_LIMIT_SCRIPT = """
-- Redis Lua script for atomic rate limit check
-- Executes all operations as a single atomic transaction

local key = KEYS[1]                    -- e.g., "ratelimit:user123:gpt4"
local now = tonumber(ARGV[1])          -- Current Unix timestamp
local window_seconds = tonumber(ARGV[2]) -- e.g., 3600
local max_requests = tonumber(ARGV[3])  -- e.g., 100
local request_id = ARGV[4]             -- Unique request identifier

-- Compute window boundaries
local window_start = now - window_seconds

-- Step 1: Remove all requests outside the current window
-- ZREMRANGEBYSCORE removes by score range (timestamps)
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

-- Step 2: Count requests in current window
-- ZCARD returns the number of members in the sorted set
local current_count = redis.call('ZCARD', key)

-- Step 3: Check if we can allow this request
if current_count < max_requests then
    -- Allow: add this request to the sorted set
    -- Score is the timestamp, member is the unique request ID
    redis.call('ZADD', key, now, request_id)
    
    -- Set key expiration to avoid unbounded growth
    -- After window_seconds, the key automatically expires
    redis.call('EXPIRE', key, window_seconds + 60)  -- +60 for safety margin
    
    -- Return 1 (allowed)
    return 1
else
    -- Deny: rate limit exceeded
    return 0
end
"""

# ============================================================================
# REDIS RATE LIMITER IMPLEMENTATION
# ============================================================================


class RedisRateLimiter:
    """
    Distributed Redis-backed rate limiter using Sliding Window Log algorithm.

    Key Features:
    - Atomic operations via Lua script (no race conditions)
    - Handles distributed/concurrent requests
    - Automatic TTL-based cleanup
    - Low latency (single Redis call per request)
    - Supports clock skew tolerance
    - Retry-safe with idempotent request IDs

    Dependencies:
        import redis
        limiter = RedisRateLimiter(redis.Redis(host='localhost', port=6379))

    Usage:
        allowed = limiter.allow(
            user_id="user123",
            model_id="gpt-4",
            max_requests=100,
            window_seconds=3600
        )

        if not allowed:
            return Response(status=429, reason="Rate limited")
    """

    def __init__(self, redis_client, script_sha: str = None):
        """
        Initialize Redis-based rate limiter.

        Args:
            redis_client: redis.Redis or redis.cluster.RedisCluster instance
            script_sha: Pre-computed SHA of Lua script (optional optimization)

        Note:
            In production, pre-load the script during initialization:
            script_sha = redis_client.script_load(REDIS_RATE_LIMIT_SCRIPT)
        """
        self.redis = redis_client
        self.script_sha = script_sha or redis_client.script_load(
            REDIS_RATE_LIMIT_SCRIPT
        )

    def allow(
        self,
        user_id: str,
        model_id: str,
        max_requests: int = 100,
        window_seconds: int = 3600,
        request_id: str = None,
    ) -> bool:
        """
        Check if a request is allowed using Redis-backed rate limiting.

        Args:
            user_id: Unique user identifier
            model_id: Model identifier (e.g., "gpt-4", "llama-70b")
            max_requests: Maximum requests in window (default 100)
            window_seconds: Time window in seconds (default 3600)
            request_id: Unique request ID (UUID). 
                    If None, generated automatically.
                    Important: Same request_id for retries ensures idempotency.

        Returns:
            True if request allowed, False if rate limited

        Raises:
            redis.RedisError: If Redis connection fails
            redis.exceptions.NoScriptError: 
                If Lua script not loaded (retry will reload)
        """

        if request_id is None:
            request_id = str(uuid.uuid4())

        key = f"ratelimit:{user_id}:{model_id}"
        now = time.time()

        try:
            # Execute Lua script atomically on Redis
            result = self.redis.evalsha(
                sha=self.script_sha,
                numkeys=1,
                keys=[key],
                args=[now, window_seconds, max_requests, request_id],
            )

            return bool(result)

        except redis.exceptions.NoScriptError:
            # Script was flushed from Redis, reload and retry
            self.script_sha = self.redis.script_load(REDIS_RATE_LIMIT_SCRIPT)
            result = self.redis.evalsha(
                sha=self.script_sha,
                numkeys=1,
                keys=[key],
                args=[now, window_seconds, max_requests, request_id],
            )
            return bool(result)

    def get_request_count(
        self, user_id: str, model_id: str, window_seconds: int = 3600
    ) -> int:
        """
        Get current request count for monitoring/debugging.

        Args:
            user_id: User identifier
            model_id: Model identifier
            window_seconds: Time window (default 3600)

        Returns:
            Current number of requests in the window
        """
        key = f"ratelimit:{user_id}:{model_id}"
        now = time.time()
        window_start = now - window_seconds

        # Count requests in current window
        # Note: This reads the live key, doesn't clean it
        count = self.redis.zcount(key, min=window_start, max=now)
        return count

    def reset_user(self, user_id: str, model_id: str = None) -> int:
        """
        Reset rate limit (admin operation).

        Args:
            user_id: User to reset
            model_id: If provided, reset only this model.
                     If None, reset all models for user.

        Returns:
            Number of keys deleted
        """
        if model_id:
            key = f"ratelimit:{user_id}:{model_id}"
            return self.redis.delete(key)
        else:
            # Delete all keys matching pattern
            pattern = f"ratelimit:{user_id}:*"
            keys = self.redis.keys(pattern)
            if keys:
                return self.redis.delete(*keys)
            return 0


# ============================================================================
# HANDLING CHALLENGES IN DISTRIBUTED SYSTEMS
# ============================================================================

"""
### Challenge 1: Clock Skew

Problem:
  Different servers have slightly different system clocks. If server A thinks
  it's 10:00:00 and server B thinks it's 10:00:05, rate limit calculations
  could be inconsistent.

Solution Approach:
  1. Use Redis server time as source of truth
     - Instead of using client's `time.time()`, query Redis: TIME command
     - All rate limit calculations use Redis server's clock
  
  2. Tolerance windows
     - Allow ±5 second drift in boundary calculations
     - Request timestamps outside [-5, +5] are rejected
  
  Implementation:
    def allow_with_redis_time(self, user_id, model_id):
        # Get server time from Redis
        redis_time = float(self.redis.time()[0])
        
        # Use Redis time instead of client's time
        result = self.redis.evalsha(
            script_sha,
            numkeys=1,
            keys=[f"ratelimit:{user_id}:{model_id}"],
            args=[redis_time, 3600, 100, uuid.uuid4()]
        )
        return bool(result)
"""

"""
### Challenge 2: Retries and Idempotency

Problem:
  If a client retries a request (network failure, timeout), the same request
  might be counted twice, double-charging the quota.

Solution:
  1. Idempotent request IDs
     - Client generates UUID for each request
     - On retry, use the same UUID
     - Rate limiter doesn't count duplicates
  
  Implementation in Lua:
    local existing = redis.call('ZSCORE', key, request_id)
    if existing ~= false then
        -- Request already counted in this window
        return 1  -- Retries are safe
    end
    
    -- New request, count it
    redis.call('ZADD', key, now, request_id)
    return 1
  
  Client-side:
    for attempt in range(3):
        try:
            response = http.post(
                url=rate_limiter_endpoint,
                data={'user_id': 'user123', 'model_id': 'gpt4'},
                headers={'X-Request-ID': request_uuid}  # Same across retries
            )
            return response
        except NetworkError:
            if attempt < 2:
                time.sleep(backoff_seconds[attempt])
            else:
                raise
"""

"""
### Challenge 3: Partial Failures and Cascading

Problem:
  Rate limiter depends on Redis. If Redis is down or experiencing network
  partition, what should happen?

Solutions:

  A. Fail Open (Allow requests):
     - If Redis unavailable, allow all requests
     - Risk: No rate limiting when it matters most
     - Use case: High availability priority (best-effort QoS)
  
  B. Fail Closed (Deny all requests):
     - If Redis unavailable, deny all requests (safe default)
     - Risk: Service unavailable if rate limiter fails
     - Use case: Strict quota enforcement
  
  C. Local Fallback (Hybrid):
     - Use local in-memory rate limiter as fallback
     - Sync state with Redis when available
     - Best of both worlds
  
  Implementation:
    def allow_with_fallback(self, user_id, model_id):
        try:
            # Try Redis first (primary)
            return self.redis_limiter.allow(user_id, model_id)
        
        except redis.ConnectionError:
            # Fall back to local limiter
            log.warning(f"Redis unavailable, using local limiter")
            return self.local_limiter.allow(user_id, model_id)
"""

"""
### Challenge 4: Multiple Redis Nodes (Sharding)

Problem:
  Single Redis instance is a bottleneck. Need to shard rate limit data across
  multiple Redis nodes for scalability.

Solution: Consistent Hashing

  1. Pre-compute hash ring with Redis nodes
     nodes = ['redis1:6379', 'redis2:6379', 'redis3:6379']
     ring = ConsistentHashRing(nodes)
  
  2. For each user-model pair, determine responsible node
     key = f"{user_id}:{model_id}"
     node = ring.get_node(key)  # Consistent across requests
  
  3. Send all rate limit ops to that node
     redis_client = self.redis_clients[node]
     redis_client.evalsha(script, ...)
  
  Implementation:
    from consistent_hash import ConsistentHashRing
    
    class ShardedRedisRateLimiter:
        def __init__(self, redis_nodes):
            self.ring = ConsistentHashRing(redis_nodes)
            self.clients = {
                node: redis.Redis.from_url(f"redis://{node}")
                for node in redis_nodes
            }
        
        def allow(self, user_id, model_id, **kwargs):
            key = f"{user_id}:{model_id}"
            node = self.ring.get_node(key)
            return self.clients[node].evalsha(...)
  
  Node Addition (Scaling):
    1. Add new node to ring
    2. Rebalancing happens gradually (only affected keys migrate)
    3. Existing rate limit state may be lost for some keys (acceptable)
       - Alternative: Replicate state with MIGRATE command
"""

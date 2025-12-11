import redis
import uuid
import time

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


class RedisRateLimiter:
    """
    Distributed Redis-backed rate limiter using Sliding Window Log algorithm.

    Dependencies:
        # Start your Redis server locally for testing:
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

    def __init__(self, redis_client):
        """
        Initialize Redis-based rate limiter.

        Args:
            redis_client: redis.Redis or redis.cluster.RedisCluster instance
        """
        self.redis = redis_client
        self.script_sha = redis_client.script_load(
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

const WORLD_ID_PATTERN =
  /^wrld_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const USER_ID_PATTERN =
  /^usr_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const VRCHAT_API_ORIGIN = "https://api.vrchat.cloud";

class UpstreamError extends Error {
  constructor(message, status = 502, retryAfter = "") {
    super(message);
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

function responseHeaders(cacheControl = "no-store") {
  return {
    "access-control-allow-origin": "*",
    "cache-control": cacheControl,
    "content-type": "application/json; charset=utf-8",
    "x-content-type-options": "nosniff",
  };
}

function json(status, payload, options = {}) {
  const headers = responseHeaders(options.cacheControl);
  if (options.cacheStatus) {
    headers["x-framepilot-cache"] = options.cacheStatus;
  }
  if (options.retryAfter) {
    headers["retry-after"] = options.retryAfter;
  }
  if (options.warning) {
    headers.warning = options.warning;
  }
  return new Response(JSON.stringify(payload), { status, headers });
}

async function responseJson(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function requireString(source, name, maximum) {
  const value = source[name];
  if (typeof value !== "string" || value.length === 0 || value.length > maximum) {
    throw new UpstreamError(`VRChat returned an invalid ${name}`);
  }
  return value;
}

function copyOptionalString(source, target, name, maximum) {
  const value = source[name];
  if (value === undefined || value === null) {
    return;
  }
  if (typeof value !== "string" || value.length > maximum) {
    throw new UpstreamError(`VRChat returned an invalid ${name}`);
  }
  target[name] = value;
}

function copyOptionalInteger(source, target, name, maximum) {
  const value = source[name];
  if (value === undefined || value === null) {
    return;
  }
  if (!Number.isInteger(value) || value < 0 || value > maximum) {
    throw new UpstreamError(`VRChat returned an invalid ${name}`);
  }
  target[name] = value;
}

function sanitizeWorld(source, expectedWorldId) {
  if (!source || typeof source !== "object") {
    throw new UpstreamError("VRChat returned an invalid world");
  }
  const worldId = requireString(source, "id", 41);
  if (worldId !== expectedWorldId || !WORLD_ID_PATTERN.test(worldId)) {
    throw new UpstreamError("VRChat returned a mismatched world");
  }
  if (source.releaseStatus !== "public") {
    throw new UpstreamError("World is not public", 404);
  }
  const world = {
    id: worldId,
    name: requireString(source, "name", 256),
    authorName: requireString(source, "authorName", 256),
    releaseStatus: "public",
  };

  if (source.authorId !== undefined && source.authorId !== null) {
    if (typeof source.authorId !== "string" || !USER_ID_PATTERN.test(source.authorId)) {
      throw new UpstreamError("VRChat returned an invalid authorId");
    }
    world.authorId = source.authorId;
  }
  for (const field of ["imageUrl", "thumbnailImageUrl"]) {
    const value = source[field];
    if (value === undefined || value === null) {
      continue;
    }
    let parsed;
    try {
      parsed = new URL(value);
    } catch {
      throw new UpstreamError(`VRChat returned an invalid ${field}`);
    }
    if (
      parsed.protocol !== "https:" ||
      parsed.hostname !== "api.vrchat.cloud" ||
      !parsed.pathname.startsWith("/api/1/")
    ) {
      throw new UpstreamError(`VRChat returned an invalid ${field}`);
    }
    world[field] = value;
  }
  for (const [field, maximum] of [
    ["visits", 10_000_000_000],
    ["favorites", 10_000_000_000],
    ["capacity", 1_000],
    ["recommendedCapacity", 1_000],
    ["version", 1_000_000],
  ]) {
    copyOptionalInteger(source, world, field, maximum);
  }
  for (const field of ["publicationDate", "labsPublicationDate", "updated_at"]) {
    copyOptionalString(source, world, field, 64);
  }
  return world;
}

function originOptions(env, method = "GET", body = undefined) {
  const headers = {
    accept: "application/json",
    "x-framepilot-origin-secret": env.ORIGIN_AUTH_SECRET,
  };
  if (body !== undefined) {
    headers["content-type"] = "application/json";
    headers["content-length"] = String(new TextEncoder().encode(body).byteLength);
  }
  return {
    method,
    headers,
    body,
    redirect: "manual",
    cf: { cacheEverything: false, cacheTtl: 0 },
  };
}

async function readVpsCache(env, worldId) {
  const response = await fetch(
    `${env.ORIGIN_URL}/v1/cache/worlds/${worldId}`,
    originOptions(env),
  );
  const payload = await responseJson(response);
  if (response.status === 200 && payload.ok && payload.world) {
    return {
      state: payload.stale ? "stale" : "hit",
      world: payload.world,
      fetchedAt: payload.fetched_at,
      expiresAt: payload.expires_at,
    };
  }
  if (response.status === 404 && payload.error === "cache_miss" && payload.known) {
    return { state: "miss" };
  }
  if (response.status === 404 && payload.error === "world_not_observed") {
    return { state: "unknown" };
  }
  throw new UpstreamError("VPS cache is unavailable");
}

async function fetchVrchatWorld(env, worldId) {
  const response = await fetch(`${VRCHAT_API_ORIGIN}/api/1/worlds/${worldId}`, {
    headers: {
      accept: "application/json",
      "user-agent": env.VRC_USER_AGENT,
    },
    redirect: "manual",
    cache: "no-store",
  });
  if (response.status === 404) {
    throw new UpstreamError("World was not found", 404);
  }
  if (response.status === 429) {
    throw new UpstreamError(
      "VRChat rate limit reached",
      429,
      response.headers.get("retry-after") || "",
    );
  }
  if (!response.ok) {
    throw new UpstreamError("VRChat API is unavailable");
  }
  return sanitizeWorld(await responseJson(response), worldId);
}

async function storeVpsCache(env, worldId, world) {
  const body = JSON.stringify(world);
  const response = await fetch(
    `${env.ORIGIN_URL}/v1/cache/worlds/${worldId}`,
    originOptions(env, "PUT", body),
  );
  const payload = await responseJson(response);
  if (!response.ok || !payload.ok) {
    throw new UpstreamError("VPS cache rejected the world");
  }
  return payload;
}

async function serveWorld(env, worldId) {
  if (!env.ORIGIN_URL || !env.ORIGIN_AUTH_SECRET || !env.VRC_USER_AGENT) {
    return json(503, { ok: false, error: "worker_not_configured" });
  }

  let cached;
  try {
    cached = await readVpsCache(env, worldId);
  } catch {
    return json(503, { ok: false, error: "vps_cache_unavailable" });
  }
  if (cached.state === "unknown") {
    return json(404, { ok: false, error: "world_not_observed" });
  }
  if (cached.state === "hit") {
    return json(
      200,
      {
        ok: true,
        world: cached.world,
        cache: {
          status: "hit",
          stored_on: "vps",
          fetched_at: cached.fetchedAt,
          expires_at: cached.expiresAt,
        },
      },
      {
        cacheControl: "public, max-age=300",
        cacheStatus: "VPS-HIT",
      },
    );
  }

  try {
    const world = await fetchVrchatWorld(env, worldId);
    try {
      const stored = await storeVpsCache(env, worldId, world);
      return json(
        200,
        {
          ok: true,
          world: stored.world,
          cache: {
            status: cached.state === "stale" ? "refreshed" : "miss",
            stored_on: "vps",
            fetched_at: stored.fetched_at,
            expires_at: stored.expires_at,
          },
        },
        {
          cacheControl: "public, max-age=300",
          cacheStatus: cached.state === "stale" ? "VPS-REFRESH" : "VPS-MISS",
        },
      );
    } catch {
      return json(
        200,
        {
          ok: true,
          world,
          cache: { status: "write_failed", stored_on: "vps" },
        },
        {
          cacheStatus: "VPS-WRITE-FAILED",
          warning: '199 FramePilot "VPS cache write failed"',
        },
      );
    }
  } catch (error) {
    if (cached.state === "stale" && cached.world) {
      return json(
        200,
        {
          ok: true,
          world: cached.world,
          cache: {
            status: "stale",
            stored_on: "vps",
            fetched_at: cached.fetchedAt,
            expires_at: cached.expiresAt,
          },
        },
        {
          cacheControl: "public, max-age=60",
          cacheStatus: "VPS-STALE",
          warning: '110 FramePilot "VRChat revalidation failed"',
        },
      );
    }
    const status = error instanceof UpstreamError ? error.status : 502;
    return json(
      status,
      {
        ok: false,
        error:
          status === 404
            ? "world_not_found"
            : status === 429
              ? "vrchat_rate_limited"
              : "vrchat_unavailable",
      },
      {
        retryAfter: error instanceof UpstreamError ? error.retryAfter : "",
      },
    );
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: {
          "access-control-allow-headers": "content-type",
          "access-control-allow-methods": "GET, OPTIONS",
          "access-control-allow-origin": "*",
          "access-control-max-age": "86400",
        },
      });
    }
    if (url.pathname === "/healthz" && request.method === "GET") {
      return json(200, {
        ok: true,
        service: "framepilot-vrc-world-proxy",
      });
    }
    const prefix = "/v1/worlds/";
    if (!url.pathname.startsWith(prefix)) {
      return json(404, { ok: false, error: "not_found" });
    }
    if (request.method !== "GET") {
      return json(405, { ok: false, error: "method_not_allowed" });
    }
    const worldId = url.pathname.slice(prefix.length);
    if (!WORLD_ID_PATTERN.test(worldId)) {
      return json(400, { ok: false, error: "invalid_world_id" });
    }
    return serveWorld(env, worldId);
  },
};

import assert from "node:assert/strict";
import test from "node:test";

import worker from "./src/index.js";


const WORLD_ID = "wrld_11111111-1111-1111-1111-111111111111";
const ENV = {
  ORIGIN_URL: "https://origin-ingest.example.com",
  ORIGIN_AUTH_SECRET: "s".repeat(64),
  VRC_USER_AGENT: "FramePilotVR/0.13.2 https://github.com/passworked/FramePilot",
};

function worldPayload() {
  return {
    id: WORLD_ID,
    name: "Test World",
    description: "This field must not leave the proxy.",
    authorId: "usr_22222222-2222-2222-2222-222222222222",
    authorName: "Test Author",
    assetUrl: "https://api.vrchat.cloud/api/1/file/secret-world-bundle",
    imageUrl: "https://api.vrchat.cloud/api/1/image/file_example/1/1024",
    thumbnailImageUrl:
      "https://api.vrchat.cloud/api/1/image/file_example/1/256",
    visits: 1234,
    favorites: 56,
    capacity: 32,
    recommendedCapacity: 16,
    releaseStatus: "public",
    publicationDate: "2025-01-01T00:00:00.000Z",
    updated_at: "2026-07-26T00:00:00.000Z",
    version: 7,
  };
}

async function requestWorld(id = WORLD_ID) {
  return worker.fetch(
    new Request(`https://framepilot-vrc-world-proxy.example/v1/worlds/${id}`),
    ENV,
  );
}

test("fresh VPS cache is returned without contacting VRChat", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (url) => {
    calls += 1;
    assert.match(String(url), /origin-ingest\.example\.com/);
    return Response.json({
      ok: true,
      known: true,
      world: worldPayload(),
      fetched_at: 100,
      expires_at: 200,
      stale: false,
    });
  };
  try {
    const response = await requestWorld();
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-framepilot-cache"), "VPS-HIT");
    assert.equal((await response.json()).cache.status, "hit");
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("cache miss fetches VRChat, strips fields, and persists to VPS", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  let stored;
  globalThis.fetch = async (url, options = {}) => {
    calls.push(String(url));
    if (String(url).includes("origin-ingest") && options.method !== "PUT") {
      return Response.json(
        { ok: false, error: "cache_miss", known: true },
        { status: 404 },
      );
    }
    if (String(url).includes("api.vrchat.cloud")) {
      assert.equal(options.headers["user-agent"], ENV.VRC_USER_AGENT);
      return Response.json(worldPayload());
    }
    stored = JSON.parse(options.body);
    assert.equal(
      options.headers["x-framepilot-origin-secret"],
      ENV.ORIGIN_AUTH_SECRET,
    );
    return Response.json({
      ok: true,
      world: stored,
      fetched_at: 100,
      expires_at: 200,
      stale: false,
    });
  };
  try {
    const response = await requestWorld();
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-framepilot-cache"), "VPS-MISS");
    assert.equal(payload.cache.status, "miss");
    assert.equal(calls.length, 3);
    assert.equal(stored.id, WORLD_ID);
    assert.equal(stored.description, undefined);
    assert.equal(stored.assetUrl, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("unobserved world IDs never contact VRChat", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return Response.json(
      { ok: false, error: "world_not_observed" },
      { status: 404 },
    );
  };
  try {
    const response = await requestWorld();
    assert.equal(response.status, 404);
    assert.equal((await response.json()).error, "world_not_observed");
    assert.equal(calls, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("stale VPS data is served when VRChat asks for backoff", async () => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async (url) => {
    calls += 1;
    if (String(url).includes("origin-ingest")) {
      return Response.json({
        ok: true,
        known: true,
        world: worldPayload(),
        fetched_at: 100,
        expires_at: 200,
        stale: true,
      });
    }
    return Response.json(
      { error: { message: "Slow down" } },
      { status: 429, headers: { "retry-after": "60" } },
    );
  };
  try {
    const response = await requestWorld();
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-framepilot-cache"), "VPS-STALE");
    assert.equal(payload.cache.status, "stale");
    assert.equal(calls, 2);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("invalid world ID is rejected before any subrequest", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    assert.fail("fetch must not be called");
  };
  try {
    const response = await requestWorld("wrld_not-valid");
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error, "invalid_world_id");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

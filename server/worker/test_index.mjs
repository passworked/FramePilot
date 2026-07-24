import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import test from "node:test";

import worker from "./src/index.js";


test("upload forwards a per-client anonymous rate key", async () => {
  const originalFetch = globalThis.fetch;
  const secret = "s".repeat(64);
  const clientIP = "203.0.113.10";
  let forwardedHeaders;
  globalThis.fetch = async (_url, options) => {
    forwardedHeaders = new Headers(options.headers);
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  try {
    const response = await worker.fetch(
      new Request(
        "https://round-darkness-4881.laptop7921.workers.dev/v1/telemetry/batches",
        {
          method: "POST",
          headers: {
            "content-type": "application/zip",
            "content-length": "3",
            "cf-connecting-ip": clientIP,
          },
          body: new Uint8Array([1, 2, 3]),
        },
      ),
      {
        ORIGIN_URL: "https://origin-ingest.example.com",
        ORIGIN_AUTH_SECRET: secret,
      },
    );

    assert.equal(response.status, 200);
    assert.equal(
      forwardedHeaders.get("x-framepilot-client-key"),
      createHmac("sha256", secret).update(clientIP).digest("hex"),
    );
    assert.notEqual(
      forwardedHeaders.get("x-framepilot-client-key"),
      createHmac("sha256", secret)
        .update("2a06:98c0:3600::103")
        .digest("hex"),
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});


test("upload without a Cloudflare client address is rejected", async () => {
  const response = await worker.fetch(
    new Request(
      "https://round-darkness-4881.laptop7921.workers.dev/v1/telemetry/batches",
      {
        method: "POST",
        headers: {
          "content-type": "application/zip",
          "content-length": "3",
        },
        body: new Uint8Array([1, 2, 3]),
      },
    ),
    {
      ORIGIN_URL: "https://origin-ingest.example.com",
      ORIGIN_AUTH_SECRET: "s".repeat(64),
    },
  );

  assert.equal(response.status, 400);
  assert.deepEqual(await response.json(), {
    ok: false,
    error: "missing_client_address",
  });
});

const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

function json(status, payload) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    },
  });
}

async function sha256Hex(buffer) {
  const digest = await crypto.subtle.digest("SHA-256", buffer);
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/healthz" && request.method === "GET") {
      const response = await fetch(`${env.ORIGIN_URL}/healthz`, {
        headers: {
          "user-agent": "framepilot-cloudflare-worker/1",
        },
        cf: { cacheTtl: 0, cacheEverything: false },
      });
      return new Response(response.body, {
        status: response.status,
        headers: {
          "content-type": "application/json",
          "cache-control": "no-store",
          "x-content-type-options": "nosniff",
        },
      });
    }
    if (url.pathname !== "/v1/telemetry/batches") {
      return json(404, { ok: false, error: "not_found" });
    }
    if (request.method !== "POST") {
      return json(405, { ok: false, error: "method_not_allowed" });
    }
    const contentType = (request.headers.get("content-type") || "")
      .split(";", 1)[0]
      .trim();
    if (!["application/zip", "application/octet-stream"].includes(contentType)) {
      return json(415, { ok: false, error: "invalid_content_type" });
    }
    const declaredLength = Number(request.headers.get("content-length") || "0");
    if (declaredLength > MAX_UPLOAD_BYTES) {
      return json(413, { ok: false, error: "upload_too_large" });
    }
    const body = await request.arrayBuffer();
    if (body.byteLength === 0 || body.byteLength > MAX_UPLOAD_BYTES) {
      return json(413, { ok: false, error: "upload_too_large" });
    }
    const actualSha256 = await sha256Hex(body);
    const suppliedSha256 = (
      request.headers.get("x-batch-sha256") || ""
    ).toLowerCase();
    if (suppliedSha256 && suppliedSha256 !== actualSha256) {
      return json(400, { ok: false, error: "sha256_mismatch" });
    }
    const originResponse = await fetch(
      `${env.ORIGIN_URL}/v1/telemetry/batches`,
      {
        method: "POST",
        headers: {
          "content-type": "application/zip",
          "content-length": String(body.byteLength),
          "x-batch-sha256": actualSha256,
          "x-framepilot-origin-secret": env.ORIGIN_SECRET,
          "cf-connecting-ip":
            request.headers.get("cf-connecting-ip") || "unknown",
          "user-agent": "framepilot-cloudflare-worker/1",
        },
        body,
        redirect: "manual",
      },
    );
    const responseHeaders = {
      "content-type": "application/json",
      "cache-control": "no-store",
      "x-content-type-options": "nosniff",
    };
    const retryAfter = originResponse.headers.get("retry-after");
    if (retryAfter) {
      responseHeaders["retry-after"] = retryAfter;
    }
    return new Response(originResponse.body, {
      status: originResponse.status,
      headers: responseHeaders,
    });
  },
};

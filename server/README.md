# FramePilot VR ingest deployment

The origin stores immutable ZIP batches under
`/srv/framepilot/objects/sha256` and indexes records in SQLite at
`/srv/framepilot/db/framepilot.sqlite3`.

The Python service listens only on `127.0.0.1:8787`. A Cloudflare Tunnel
publishes a dedicated origin hostname, while the public Worker injects the
origin secret. Direct uploads to the tunnel hostname are rejected.
The Worker also converts the visitor address into an HMAC rate-limit key
before forwarding the request. The origin uses that opaque key to keep
contributors on separate limits without storing their source addresses.

Privacy-sensitive fields, malformed archives, oversized uploads, invalid
hashes, and unsupported schemas are rejected before indexing.
Hardware analysis fields are limited to GPU/HMD names, GPU VRAM, CPU model
and core/thread counts, and total system RAM. Device serial numbers, machine
names, and raw hardware identifiers are not accepted.

The local backup timer creates consistent SQLite backups and keeps 14 days.
The object directory still requires an off-server backup target for disaster
recovery.

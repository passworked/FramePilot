# FramePilot VR project constraints

## Version maintenance

The project uses semantic versions in the form `MAJOR.MINOR.PATCH`. Every
agent that changes the project must assess the version impact before handing
off the result and maintain the version automatically.

1. `MAJOR`（主版本号）may be increased only when the user explicitly requests
   a major-version release. Never infer or perform a major-version increase
   from the size of a change alone.
2. `MINOR`（附版本号）must be increased when a release adds a clear new
   capability or user-facing feature. Reset `PATCH` to `0`.
3. `PATCH`（修订号）must be increased for bug fixes, reliability fixes,
   ordinary maintenance, wording changes, and UI/layout adjustments that do
   not add a clear new capability.
4. Documentation-only or project-constraint-only edits do not require a
   version increase unless the user asks for a new release artifact.
5. When one release contains multiple kinds of changes, apply only the
   highest required increase once. Do not increase the version repeatedly for
   individual files or commits in the same release task.
6. An explicit version requested by the user overrides the automatic choice,
   except that an agent must not silently convert it into a different major
   version.

`pyproject.toml` is the canonical version source. Whenever the version changes,
synchronize all user-visible copies, including:

- `steamvr_adaptive_gui.py` (`APP_VERSION`)
- `快速开始-Quick-Start.txt`
- `START_HERE.html`
- release archive and installer filenames

Before delivery, search for stale copies of the previous version, verify that
all active version strings agree, and name newly built artifacts with the
updated version.

## GitHub delivery

After each feature is successfully implemented and its relevant checks pass,
commit that feature once and push the commit to the configured private GitHub
remote automatically. Do not wait for a separate commit or push request.

Keep each feature commit scoped and identifiable. Never commit build outputs,
release archives, runtime logs, local settings, credentials, tokens, secrets,
or unrelated user changes. If GitHub authentication or the remote is
unavailable, preserve the completed local work and report the publishing
blocker instead of discarding or rewriting it.

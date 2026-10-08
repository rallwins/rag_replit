---
name: Python service launch root
description: Workspace-root resolution for Python processes launched from the bundled API artifact.
---

When the API artifact starts a Python service, resolve the workspace root from the emitted Node bundle rather than assuming the workflow's current directory is the repository root.

**Why:** The managed workflow runs from the API artifact, while `python_api` is a root-level package; resolving one directory too high leaves Python unable to import it.

**How to apply:** Keep child-process working-directory logic aligned with the built bundle location and verify imports through the managed workflow, not only with a root-level shell command.

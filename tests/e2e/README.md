# Two-Blender end-to-end tests

These run two real Blender windows on one machine over a loopback TLS session.
They are separate scripts so a failure points at one feature; each writes a JSON
result and exits non-zero on mismatch.

| Script | Covers |
|---|---|
| `run_material.ps1` | material node graph, packed image transfer, colour space, live material edits |
| `run_details.ps1` | parenting + `matrix_parent_inverse`, modifiers on a non-active object, collections/visibility, object colour |
| `run_topology.ps1` | two Edit Mode subdivide operations rebuilt on the client with matching counts |

Requirements:

- Blender 5.x installed at the default path, or pass `-Blender <path>`.
- The cp313 vendor tree at `%LOCALAPPDATA%\Blendermp\python` (installed by the app).
- PowerShell 5+.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests/e2e/run_material.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests/e2e/run_details.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tests/e2e/run_topology.ps1
```

Each runner cleans up only its own Blender processes, uses deterministic ports
(52355/52365/52375), and stores logs/results under `%TEMP%\bmp-*`.

# Two-Blender end-to-end tests

These run two real Blender windows on one machine over a loopback TLS session.
They are separate scripts so a failure points at one feature; each writes a JSON
result and exits non-zero on mismatch.

| Script | Covers |
|---|---|
| `run_material.ps1` | material node graph, packed image transfer, colour space, live material edits |
| `run_details.ps1` | parenting + `matrix_parent_inverse`, modifiers on a non-active object, collections/visibility, object colour |
| `run_topology.ps1` | two Edit Mode subdivide operations rebuilt on the client with matching counts |
| `run_resume.ps1` | forced socket drop, resume without scene reset, restored leases and offline transform replay |
| `run_checkpoint.ps1` | checkpoint save, live restore, peer scene reset and restored transform |
| `run_undo.ps1` | scoped undo of a local transform, verified on both sides |
| `run_animation.ps1` | armature bones, vertex weights, armature modifier, shape keys, action curves and live pose |
| `run_sculpt.ps1` | Sculpt Mode vertex deltas streamed to the client |

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

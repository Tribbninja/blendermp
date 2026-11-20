# Performance harness

Run inside Blender 5.x:

```powershell
& "C:\Program Files\Blender Foundation\Blender 5.3\blender.exe" `
  --background --factory-startup --python bench/bench_mesh.py -- --write-baseline
```

Options:

- `--sizes 10000,100000,1000000` — vertex counts to measure (default).
- `--repeat 3` — median samples for codec timings.
- `--write-baseline` — replace `baseline.json`.
- `--compare` — fail when a metric is more than 20% slower than the baseline.

Metrics per size: `read_ms` (Blender vertex read), delta `encode/decode/apply`,
and for meshes up to 250k vertices a full geometry `apply_ms` and BMesh
`snapshot_ms`. Material graph serialize/sanitize/apply is measured once.

`baseline.json` is machine-specific, so only compare on the same machine (or a
CI runner with a recorded baseline). `results.json` is overwritten on each run.

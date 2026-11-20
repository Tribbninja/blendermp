# Cross-engine bridge

Blendermp can stream a live Blender scene to another engine over a local TCP
socket. The bridge is **one-way**: Blender is authoritative, the engine renders.

## Start the bridge

1. In Blender, open the Blendermp N-panel.
2. Under **Cross-engine Bridge**, choose Unity, Unreal or Godot.
3. Set the port (default `7103`) and press **Start**.
4. Connect the matching receiver below.

The default bind address is `127.0.0.1`, so only this machine can connect. For
a LAN receiver, change `BridgeServer(host=...)` in
`addon/blender_multiplayer/core/cross_engine.py` (and open the firewall port).

## Receivers

| Engine | File | Notes |
|---|---|---|
| Unity | `unity/BlendermpReceiver.cs` | Drop into `Assets/`, add to an empty GameObject, press Play. |
| Unreal | `unreal/blendermp_receiver.py` | Run in the Output Log Python console. Spawns scaled cube placeholders. |
| Godot 4 | `godot/blendermp_receiver.gd` | Attach to a `Node3D`, run the project. Builds triangle meshes. |
| Python | `python/console_receiver.py` | Prints every message; useful for debugging. |

## Wire format

Newline-delimited JSON over TCP. Every message has `type`, `seq` and `t`.

```json
{"type":"hello","protocol":1,"engine":"unity","app":"Blendermp"}
{"type":"object","uid":"…","name":"Cube","kind":"mesh",
 "matrix":[16 floats, Blender row-major world matrix],
 "position":[x,y,z],"rotation":[x,y,z,w],"scale":[x,y,z],
 "bounds":[0,0,0,dx,dy,dz],"visible":true,
 "metadata":{"type":"MESH","materials":["Red"],"parent":null},
 "mesh":{"positions":[…],"normals":[…],"indices":[…]}}
{"type":"remove","uid":"…"}
{"type":"scene","engine":"unity","objects":[…],"materials":[…]}
{"type":"materials","materials":[{"name":"Red","color":[r,g,b,a],…}]}
```

* `matrix` is the raw Blender world matrix (row-major, 16 floats).
* `position` / `rotation` / `scale` are pre-converted for Y-up engines
  (Unity, Godot): Blender `(x, y, z)` becomes `(x, z, -y)`.
* `mesh.positions` and `mesh.normals` are local-space triples; `indices` are
  triangle indices. Meshes are capped at 20,000 vertices and are sent once per
  object (on first appearance), not on every transform tick.
* `materials` are sent every two seconds, so colour edits propagate.

## Limits

* Meshes above the cap are sent as transform-only objects.
* Modifiers are evaluated (`evaluated_get`) before export.
* Armatures, shape keys and animations are not exported yet.
* Unreal's receiver uses engine cube placeholders; the raw mesh arrays are in
  each message for a project-specific procedural-mesh adapter.

The contract lives in `addon/blender_multiplayer/core/cross_engine.py` and is
covered by `tests/test_cross_engine.py`.

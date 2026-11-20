extends Node3D

## Blendermp cross-engine receiver for Godot 4.
##
## Attach this script to a Node3D in your scene, run the project, and start the
## Blendermp bridge in Blender (N-panel > Cross-engine Bridge > Start). The node
## connects over TCP, reads newline-delimited JSON and mirrors transforms,
## visibility, colours and triangle meshes.

@export var host: String = "127.0.0.1"
@export var port: int = 7103
@export var world_scale: float = 1.0
@export var reconnect_delay: float = 1.0

var _peer := StreamPeerTCP.new()
var _buffer := ""
var _connected := false
var _retry_at := 0.0
var _objects: Dictionary = {}
var _materials: Dictionary = {}


func _ready() -> void:
	_connect()


func _connect() -> void:
	_peer = StreamPeerTCP.new()
	var error := _peer.connect_to_host(host, port)
	if error != OK:
		_retry_at = Time.get_ticks_msec() / 1000.0 + reconnect_delay
		return
	_connected = false


func _process(_delta: float) -> void:
	var now := Time.get_ticks_msec() / 1000.0
	var status := _peer.get_status()
	if status == StreamPeerTCP.STATUS_CONNECTED:
		_connected = true
		var available := _peer.get_available_bytes()
		if available > 0:
			var chunk := _peer.get_utf8_string(available)
			_buffer += chunk
			_consume()
	elif status == StreamPeerTCP.STATUS_ERROR or status == StreamPeerTCP.STATUS_NONE:
		if _connected:
			_connected = false
			_buffer = ""
		if now >= _retry_at:
			_retry_at = now + reconnect_delay
			_connect()


func _consume() -> void:
	while true:
		var newline := _buffer.find("\n")
		if newline < 0:
			break
		var line := _buffer.substr(0, newline).strip_edges()
		_buffer = _buffer.substr(newline + 1)
		if line.is_empty():
			continue
		var parsed = JSON.parse_string(line)
		if typeof(parsed) == TYPE_DICTIONARY:
			_apply(parsed)


func _apply(message: Dictionary) -> void:
	match str(message.get("type", "")):
		"scene":
			for material in message.get("materials", []):
				_store_material(material)
			for item in message.get("objects", []):
				_apply_object(item)
		"materials":
			for material in message.get("materials", []):
				_store_material(material)
		"object":
			_apply_object(message)
		"remove":
			_remove_object(str(message.get("uid", "")))


func _store_material(data: Dictionary) -> void:
	var name := str(data.get("name", ""))
	var color = data.get("color", data.get("albedo", [1, 1, 1, 1]))
	if name.is_empty() or typeof(color) != TYPE_ARRAY or color.size() < 3:
		return
	_materials[name] = Color(float(color[0]), float(color[1]), float(color[2]), 1.0 if color.size() < 4 else float(color[3]))


func _apply_object(message: Dictionary) -> void:
	var uid := str(message.get("uid", ""))
	if uid.is_empty():
		return
	var node: Node3D = _objects.get(uid)
	if node == null or not is_instance_valid(node):
		if str(message.get("kind", "")) == "mesh":
			node = MeshInstance3D.new()
		else:
			node = Node3D.new()
		add_child(node)
		_objects[uid] = node
	node.name = str(message.get("name", uid))
	node.visible = bool(message.get("visible", true))

	var position = message.get("position", [0, 0, 0])
	var rotation = message.get("rotation", [0, 0, 0, 1])
	var scale = message.get("scale", [1, 1, 1])
	if typeof(position) == TYPE_ARRAY and position.size() >= 3:
		var origin := Vector3(float(position[0]), float(position[1]), float(position[2])) * world_scale
		var basis := Basis(Quaternion(float(rotation[0]), float(rotation[1]), float(rotation[2]), float(rotation[3])))
		basis = basis.scaled(Vector3(float(scale[0]), float(scale[1]), float(scale[2])) * world_scale)
		node.transform = Transform3D(basis, origin)

	var mesh = message.get("mesh")
	if mesh is Dictionary and node is MeshInstance3D:
		_apply_mesh(node, mesh)

	if node is MeshInstance3D:
		var material_name := ""
		var names = message.get("metadata", {}).get("materials", [])
		if typeof(names) == TYPE_ARRAY and names.size() > 0:
			material_name = str(names[0])
		if _materials.has(material_name):
			var material := StandardMaterial3D.new()
			material.albedo_color = _materials[material_name]
			node.material_override = material


func _apply_mesh(node: MeshInstance3D, mesh: Dictionary) -> void:
	var positions = mesh.get("positions", [])
	var indices = mesh.get("indices", [])
	if typeof(positions) != TYPE_ARRAY or positions.size() < 9:
		return
	if typeof(indices) != TYPE_ARRAY or indices.size() < 3:
		return
	var vertices := PackedVector3Array()
	var index := 0
	while index + 2 < positions.size():
		vertices.append(
			Vector3(float(positions[index]), float(positions[index + 2]), -float(positions[index + 1]))
		)
		index += 3
	var triangles := PackedInt32Array()
	for value in indices:
		triangles.append(int(value))
	var arrays := []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = vertices
	arrays[Mesh.ARRAY_INDEX] = triangles
	var array_mesh := ArrayMesh.new()
	array_mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	node.mesh = array_mesh


func _remove_object(uid: String) -> void:
	if uid.is_empty() or not _objects.has(uid):
		return
	var node = _objects[uid]
	if is_instance_valid(node):
		node.queue_free()
	_objects.erase(uid)

"""Material payloads.

The payload carries the common Principled BSDF controls (base colour,
metallic, roughness, emission) and the material viewport colour, plus an
optional whitelisted shader graph for the surface output. The graph form is
deliberately narrow: image textures, texture coordinates/mapping, normal and
bump nodes, colour/vector maths and a handful of procedural textures. Node
groups, scripts and other unsafe node types are never accepted.

Values are always resolved on the receiving side by node *index* and socket
*identifier*, so node names only affect the UI.
"""

from __future__ import annotations

import math

MAX_NODES = 64
MAX_LINKS = 192
MAX_INPUTS = 40
MAX_PROPS = 32
MAX_TEXT = 64

# Exact ``bl_idname`` allow-list. Anything outside it is rejected rather than
# silently dropped, so a corrupt or hostile graph cannot half-apply.
ALLOWED_NODES = {
    "ShaderNodeOutputMaterial",
    "ShaderNodeBsdfPrincipled",
    "ShaderNodeBsdfDiffuse",
    "ShaderNodeBsdfGlossy",
    "ShaderNodeBsdfTransparent",
    "ShaderNodeBsdfRefraction",
    "ShaderNodeBsdfAnisotropic",
    "ShaderNodeBsdfSheen",
    "ShaderNodeBsdfToon",
    "ShaderNodeBsdfVelvet",
    "ShaderNodeBsdfMetallic",
    "ShaderNodeEmission",
    "ShaderNodeHoldout",
    "ShaderNodeTexImage",
    "ShaderNodeTexEnvironment",
    "ShaderNodeTexCoord",
    "ShaderNodeMapping",
    "ShaderNodeNormalMap",
    "ShaderNodeBump",
    "ShaderNodeMix",
    "ShaderNodeMixRGB",
    "ShaderNodeValToRGB",
    "ShaderNodeGamma",
    "ShaderNodeHueSaturation",
    "ShaderNodeBrightContrast",
    "ShaderNodeInvert",
    "ShaderNodeMapRange",
    "ShaderNodeMath",
    "ShaderNodeVectorMath",
    "ShaderNodeSeparateColor",
    "ShaderNodeCombineColor",
    "ShaderNodeSeparateXYZ",
    "ShaderNodeCombineXYZ",
    "ShaderNodeTexNoise",
    "ShaderNodeTexVoronoi",
    "ShaderNodeTexWave",
    "ShaderNodeTexChecker",
    "ShaderNodeTexGradient",
    "ShaderNodeTexBrick",
    "ShaderNodeTexMagic",
    "ShaderNodeTexSky",
    "ShaderNodeFresnel",
    "ShaderNodeLayerWeight",
    "ShaderNodeAttribute",
    "ShaderNodeUVMap",
    "ShaderNodeObjectInfo",
    "ShaderNodeTangent",
    "ShaderNodeBlackbody",
    "ShaderNodeWavelength",
    "ShaderNodeLightPath",
    "ShaderNodeCameraData",
    "ShaderNodeNewGeometry",
}

ALLOWED_PROPS = {
    "interpolation",
    "extension",
    "projection",
    "uv_map",
    "vector_type",
    "attribute_name",
    "attribute_type",
    "noise_dimensions",
    "feature",
    "distance",
    "voronoi_dimensions",
    "wave_type",
    "bands_direction",
    "wave_profile",
    "data_type",
    "blend_type",
    "clamp_factor",
    "clamp",
    "space",
    "sky_type",
    "sun_direction",
    "altitude",
    "air_density",
    "dust_density",
    "ozone_density",
    "operation",
    "use_clamp",
    "elements",
}


def _color(value, size: int):
    if not isinstance(value, (list, tuple)) or len(value) < size:
        return None
    result = []
    for component in value[:size]:
        try:
            number = float(component)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return None
        result.append(number)
    return result


def _number(value, low: float, high: float):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not low <= number <= high:
        return None
    return number


def _text(value, limit: int = MAX_TEXT):
    if not isinstance(value, str):
        return None
    return value[:limit]


def _scalar(value):
    """A finite number or a small enum/string, safe for a node socket."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:32]
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > 1.0e6:
        return None
    return number


def _socket_value(value):
    if isinstance(value, (list, tuple)):
        if not 1 <= len(value) <= 4:
            return None
        result = [_scalar(component) for component in value]
        if any(component is None for component in result):
            return None
        return result
    scalar = _scalar(value)
    return scalar


def _graph(value) -> tuple[list | None, list | None]:
    if value is None:
        return [], []
    if not isinstance(value, list) or len(value) > MAX_NODES:
        return None, None
    nodes = []
    for spec in value:
        if not isinstance(spec, dict):
            return None, None
        node_type = spec.get("bl_idname")
        if not isinstance(node_type, str) or node_type not in ALLOWED_NODES:
            return None, None
        name = _text(spec.get("name", ""))
        if name is None:
            return None, None
        node = {"bl_idname": node_type, "name": name[:MAX_TEXT]}
        loc = spec.get("location")
        if isinstance(loc, (list, tuple)) and len(loc) == 2:
            location = [_scalar(component) for component in loc]
            if any(component is None for component in location):
                return None, None
            node["location"] = location
        inputs = spec.get("inputs", {})
        if not isinstance(inputs, dict) or len(inputs) > MAX_INPUTS:
            return None, None
        clean_inputs = {}
        for key, raw in inputs.items():
            key = _text(key)
            if key is None:
                return None, None
            clean = _socket_value(raw)
            if clean is None:
                return None, None
            clean_inputs[key] = clean
        if clean_inputs:
            node["inputs"] = clean_inputs
        props = spec.get("props", {})
        if not isinstance(props, dict) or len(props) > MAX_PROPS:
            return None, None
        clean_props = {}
        for key, raw in props.items():
            if key not in ALLOWED_PROPS:
                return None, None
            if key == "elements":
                if node_type != "ShaderNodeValToRGB" or not isinstance(raw, list) or len(raw) > 32:
                    return None, None
                elements = []
                for element in raw:
                    if not isinstance(element, (list, tuple)) or len(element) != 5:
                        return None, None
                    position = _number(element[0], 0.0, 1.0)
                    color = _color(element[1:5], 4)
                    if position is None or color is None:
                        return None, None
                    elements.append([position, *color])
                clean_props["elements"] = elements
                continue
            clean = _socket_value(raw)
            if clean is None:
                return None, None
            clean_props[key] = clean
        if clean_props:
            node["props"] = clean_props
        image = spec.get("image")
        if image is not None:
            image = _text(image)
            if image is None:
                return None, None
            node["image"] = image
        image_hash = spec.get("image_hash")
        if image_hash is not None:
            image_hash = _text(image_hash, 40)
            if image_hash is None or len(image_hash) != 40:
                return None, None
            node["image_hash"] = image_hash
        colorspace = spec.get("image_colorspace")
        if colorspace is not None:
            colorspace = _text(colorspace, 32)
            if colorspace is None:
                return None, None
            node["image_colorspace"] = colorspace
        alpha_mode = spec.get("image_alpha_mode")
        if alpha_mode is not None:
            alpha_mode = _text(alpha_mode, 16)
            if alpha_mode is None:
                return None, None
            node["image_alpha_mode"] = alpha_mode
        nodes.append(node)
    return nodes, None


def _sanitize_links(value, node_count: int):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_LINKS:
        return None
    links = []
    for link in value:
        if not isinstance(link, (list, tuple)) or len(link) != 4:
            return None
        source, source_socket, target, target_socket = link
        if not isinstance(source, int) or not isinstance(target, int):
            return None
        if not 0 <= source < node_count or not 0 <= target < node_count:
            return None
        if source == target:
            return None
        source_socket = _text(source_socket)
        target_socket = _text(target_socket)
        if source_socket is None or target_socket is None:
            return None
        links.append([source, source_socket, target, target_socket])
    return links


def sanitize(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    name = value.get("name", "Material")
    if not isinstance(name, str):
        return None
    name = name.strip()[:MAX_TEXT] or "Material"
    base = _color(value.get("base_color", [0.8, 0.8, 0.8, 1.0]), 4)
    emission = _color(value.get("emission_color", [0.0, 0.0, 0.0]), 3)
    metallic = _number(value.get("metallic", 0.0), 0.0, 1.0)
    roughness = _number(value.get("roughness", 0.5), 0.0, 1.0)
    strength = _number(value.get("emission_strength", 0.0), 0.0, 100.0)
    if base is None or emission is None or metallic is None or roughness is None or strength is None:
        return None
    result = {
        "name": name,
        "base_color": base,
        "metallic": metallic,
        "roughness": roughness,
        "emission_color": emission,
        "emission_strength": strength,
    }
    raw_nodes = value.get("nodes")
    if raw_nodes:
        nodes, _unused = _graph(raw_nodes)
        if nodes is None:
            return None
        links = _sanitize_links(value.get("links"), len(nodes))
        if links is None:
            return None
        result["nodes"] = nodes
        result["links"] = links
    return result

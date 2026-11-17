import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
import blender_multiplayer as bmp

mesh = bpy.data.meshes.new("m")
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)], [], [(0,1,2)])
mesh.update()
obj = bpy.data.objects.new("MaterialTarget", mesh)
bpy.context.scene.collection.objects.link(obj)
obj["bmp_uuid"] = "12345678-1234-5678-1234-567812345678"
payload = {
    "name": "Remote Red",
    "base_color": [0.9, 0.1, 0.05, 1.0],
    "metallic": 0.3,
    "roughness": 0.2,
    "emission_color": [0.0, 0.0, 0.0],
    "emission_strength": 0.0,
}
bmp._apply_material(bpy.context, {"uuid": obj["bmp_uuid"], "slot": 0, "material": payload})
mat = obj.material_slots[0].material
print("material", mat.name, mat is not None)
values = bmp.material_values(mat)
print("roundtrip", values["base_color"][:3], values["metallic"], values["roughness"])
print("BMP_MATERIAL_OK")

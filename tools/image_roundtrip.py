import sys
sys.path.insert(0, r"C:\Users\Isaac\Desktop\blender multiplayer\addon")
import bpy
import blender_multiplayer as bmp

with open(r"C:\Users\Isaac\Desktop\blender multiplayer\docs\ui-preview.jpeg", "rb") as handle:
    data = handle.read()
bmp._apply_image(bpy.context, {"name": "Preview", "format": "jpg", "data": data})
image = bpy.data.images.get("Preview")
print("image", bool(image), image.size[:] if image else None, bool(image.packed_file) if image else None)
print("BMP_IMAGE_OK")

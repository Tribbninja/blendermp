"""N-panel UI for the addon."""

from __future__ import annotations

import bpy


def _state_icon(state: str) -> str:
    return {
        "offline": "RADIOBUT_OFF",
        "hosting": "RADIOBUT_ON",
        "joining": "SORTTIME",
        "connected": "CHECKMARK",
    }.get(state, "QUESTION")


class BMP_PT_main(bpy.types.Panel):
    bl_label = "Blendermp"
    bl_idname = "BMP_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Blendermp"

    def draw(self, context):
        layout = self.layout
        settings = context.window_manager.bmp

        header = layout.row(align=True)
        header.label(text=settings.state.title(), icon=_state_icon(settings.state))
        if settings.is_host:
            header.label(text="host", icon="HOME")
        header.label(text=f"{settings.rtt_ms} ms" if settings.rtt_ms else "")

        if settings.state in {"hosting", "connected"}:
            box = layout.box()
            box.label(text="Room code", icon="LINKED")
            row = box.row(align=True)
            row.scale_y = 1.4
            row.label(text=settings.code)
            row.operator("bmp.copy_code", text="", icon="COPYDOWN")
            if settings.verify_code:
                box.label(text=f"Verify: {settings.verify_code}", icon="LOCKED")
        else:
            box = layout.box()
            box.label(text="Join a room", icon="LINKED")
            row = box.row(align=True)
            row.prop(settings, "join_code", text="")
            row.operator("bmp.join", text="Join", icon="IMPORT")

        row = layout.row(align=True)
        if settings.state == "offline":
            row.operator("bmp.host", text="Host", icon="PLAY")
            row.operator("bmp.join", text="Join", icon="IMPORT")
        else:
            row.operator("bmp.leave", text="Leave", icon="PANEL_CLOSE")
            if settings.state == "connected":
                row.operator("bmp.ping", text="Ping", icon="SORTTIME")
            row.operator("bmp.ping_mark", text="Ping cursor", icon="CURSOR")

        if settings.last_error:
            error = layout.box()
            error.alert = True
            error.label(text=settings.last_error[:180], icon="ERROR")

        if settings.state != "offline":
            box = layout.box()
            box.label(text=f"People ({len(settings.users)})", icon="COMMUNITY")
            for user in settings.users:
                row = box.row(align=True)
                row.label(text=user.name, icon="USER")
                if user.host:
                    row.label(text="host", icon="HOME")
                row.label(text=f"{user.ping_ms} ms" if user.ping_ms else "")

        layout.separator()
        box = layout.box()
        box.label(text="Sync", icon="MODIFIER")
        box.prop(settings, "sync_transforms")
        box.prop(settings, "sync_mesh")
        box.prop(settings, "sync_materials")
        box.prop(settings, "sync_modifiers")
        box.prop(settings, "paused")
        box.prop(settings, "follow_host")
        row = box.row(align=True)
        row.operator("bmp.track_mesh", text="Track selected", icon="MESH_DATA")
        row.operator("bmp.untrack_mesh", text="", icon="X")
        lock_row = box.row(align=True)
        lock_row.operator("bmp.request_edit", text="Request edit", icon="UNLOCKED")
        lock_row.operator("bmp.grant_edit", text="Grant", icon="LOCKED")
        box.operator("bmp.share_images", text="Share images", icon="IMAGE_DATA")

        if settings.state != "offline":
            box = layout.box()
            box.label(text="Chat", icon="OUTLINER_OB_FONT")
            column = box.column()
            for message in list(settings.messages)[-6:]:
                column.label(text=f"{message.author}: {message.text}"[:120])
            row = box.row(align=True)
            row.prop(settings, "chat_text", text="")
            row.operator("bmp.send_chat", text="", icon="PLAY")

        box = layout.box()
        box.label(text="Advanced", icon="PREFERENCES")
        box.prop(settings, "port")
        box.prop(settings, "pin")
        box.prop(settings, "name")
        box.prop(settings, "use_relay")
        if settings.use_relay:
            box.prop(settings, "relay_host")
            box.prop(settings, "relay_port")
        if settings.fingerprint:
            box.label(text="Host certificate", icon="LOCKED")
            box.label(text=settings.fingerprint[:32])
            box.label(text=settings.fingerprint[32:])

        column = layout.column(align=True)
        column.operator("bmp.export_log", text="Export diagnostics", icon="TEXT")


classes = (BMP_PT_main,)


def register() -> None:
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister() -> None:
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

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
        header.label(text=settings.role.title() if settings.role else "")
        header.label(text=f"{settings.rtt_ms} ms" if settings.rtt_ms else "")

        if settings.state in {"hosting", "connected"}:
            box = layout.box()
            box.label(text="Server", icon="WORLD")
            if settings.is_host:
                box.label(text="Public listing is managed in the desktop app")
            if settings.verify_code:
                box.label(text=f"Verify: {settings.verify_code}", icon="LOCKED")
            if settings.code:
                box.operator("bmp.copy_invite", text="Copy invite link", icon="URL")

        row = layout.row(align=True)
        if settings.state == "offline":
            row.label(text="Host or browse public servers in the desktop app", icon="INFO")
        else:
            row.operator("bmp.leave", text="Leave", icon="PANEL_CLOSE")
            if settings.state == "connected":
                row.operator("bmp.ping", text="Ping", icon="SORTTIME")
            row.operator("bmp.ping_mark", text="Ping cursor", icon="CURSOR")
            row.operator("bmp.laser", text="Laser", icon="CURSOR")
            row.operator("bmp.undo_shared", text="Undo my edit", icon="LOOP_BACK")

        if settings.last_error:
            error = layout.box()
            error.alert = True
            label = f"[{settings.last_error_code}] {settings.last_error}" if settings.last_error_code else settings.last_error
            error.label(text=label[:180], icon="ERROR")

        if settings.conflict_count:
            conflict = layout.box()
            conflict.alert = True
            conflict.label(text=settings.conflict_info or "Offline edits conflict", icon="ERROR")
            row = conflict.row(align=True)
            row.operator("bmp.conflict_keep_mine", text="Keep mine", icon="CHECKMARK")
            row.operator("bmp.conflict_take_host", text="Take host", icon="IMPORT")

        if settings.crash_notice:
            crash = layout.box()
            crash.alert = True
            crash.label(text="Blendermp closed unexpectedly last time", icon="RECOVER_LAST")
            crash.label(text=settings.crash_notice[:180])
            row = crash.row(align=True)
            row.operator("bmp.recover_checkpoints", text="Recover", icon="FILE_REFRESH")
            row.operator("bmp.dismiss_crash", text="Dismiss", icon="X")
            if settings.journal_available:
                crash.operator("bmp.restore_journal", text="Restore offline edits", icon="FILE_TEXT")

        if settings.state != "offline":
            box = layout.box()
            box.label(text=f"People ({len(settings.users)})", icon="COMMUNITY")
            for user in settings.users:
                row = box.row(align=True)
                row.label(text=user.name, icon="USER")
                if user.host:
                    row.label(text="host", icon="HOME")
                row.label(text=f"{user.ping_ms} ms" if user.ping_ms else "")
                if user.name != settings.name and user.has_view:
                    follow = row.operator("bmp.follow_user", text="", icon="CAMERA_DATA")
                    follow.name = user.name
                    if settings.follow_user == user.name:
                        row.label(text="following", icon="CHECKMARK")
                if user.name != settings.name:
                    send = row.operator("bmp.bundle_send", text="", icon="EXPORT")
                    send.target = user.name
            if settings.overlay_info:
                box.label(text=settings.overlay_info, icon="HIDE_OFF")

        layout.separator()
        box = layout.box()
        box.label(text="Recovery", icon="RECOVER_LAST")
        row = box.row(align=True)
        row.operator("bmp.checkpoint_create", text="Checkpoint", icon="FILE_TICK")
        if settings.is_host or settings.state == "offline":
            row.operator("bmp.checkpoint_restore", text="Restore", icon="FILE_REFRESH")

        layout.separator()
        box = layout.box()
        box.label(text="Sync", icon="MODIFIER")
        box.prop(settings, "sync_transforms")
        box.prop(settings, "sync_mesh")
        box.prop(settings, "sync_materials")
        box.prop(settings, "sync_modifiers")
        box.prop(settings, "sync_visible_only")
        box.operator("bmp.toggle_mute", text="Mute active object", icon="HIDE_ON")
        box.prop(settings, "paused")
        box.prop(settings, "follow_host")
        box.prop(settings, "show_cameras")
        box.prop(settings, "show_selections")
        box.prop(settings, "show_labels")
        box.prop(settings, "show_lasers")
        box.prop(settings, "overlay_opacity")
        box.prop(settings, "overlay_scale")
        box.prop(settings, "colorblind_safe")
        row = box.row(align=True)
        row.operator("bmp.track_mesh", text="Track selected", icon="MESH_DATA")
        row.operator("bmp.untrack_mesh", text="", icon="X")
        lock_row = box.row(align=True)
        lock_row.operator("bmp.request_edit", text="Request edit", icon="UNLOCKED")
        lock_row.operator("bmp.grant_edit", text="Grant", icon="LOCKED")
        if settings.lease_requester:
            lock_row.operator("bmp.deny_edit", text="Deny", icon="CANCEL")
        if settings.lease_info:
            box.label(text=settings.lease_info[:120], icon="LOCKED")
        box.operator("bmp.share_images", text="Share images", icon="IMAGE_DATA")

        box = layout.box()
        box.label(text="Collaboration", icon="COMMUNITY")
        box.prop(settings, "bookmark_name")
        row = box.row(align=True)
        row.operator("bmp.bookmark_save", text="Save view", icon="BOOKMARKS")
        row.operator("bmp.copy_invite", text="Invite link", icon="URL")
        if settings.bookmarks:
            column = box.column(align=True)
            for entry in settings.bookmarks:
                item = column.row(align=True)
                item.label(text=entry.name[:32])
                jump = item.operator("bmp.bookmark_jump", text="", icon="CAMERA_DATA")
                jump.name = entry.name
                if settings.is_host or settings.role in {"admin", "owner"}:
                    push = item.operator("bmp.bookmark_push", text="", icon="FORWARD")
                    push.name = entry.name
                remove = item.operator("bmp.bookmark_delete", text="", icon="X")
                remove.name = entry.name
        box.prop(settings, "apply_bookmarks")
        if settings.bundle_count:
            offer = box.box()
            offer.alert = True
            offer.label(
                text=f"Bundle from {settings.bundle_sender or 'teammate'}: {settings.bundle_info}"[:150],
                icon="IMPORT",
            )
            row = offer.row(align=True)
            accept = row.operator("bmp.bundle_accept", text="Accept", icon="CHECKMARK")
            accept.bundle = settings.bundle_id
            dismiss = row.operator("bmp.bundle_dismiss", text="Dismiss", icon="X")
            dismiss.bundle = settings.bundle_id

        if settings.transfer_active:
            transfer = layout.box()
            transfer.label(text=settings.transfer_info[:150], icon="IMPORT")
            transfer.label(text=f"{settings.transfer_percent:.0f}%")

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

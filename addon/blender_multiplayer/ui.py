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

        if settings.pending_fingerprint:
            warning = layout.box()
            warning.alert = True
            warning.label(text="Host certificate changed", icon="ERROR")
            warning.label(text=f"New code: {settings.verify_code or '?'}")
            warning.label(text="Compare it with the host before trusting")
            warning.operator(
                "bmp.trust_host", text="Trust new certificate", icon="CHECKMARK"
            )

        if settings.state in {"hosting", "connected"}:
            box = layout.box()
            box.label(text="Server", icon="WORLD")
            if settings.is_host:
                box.label(text="Public listing is managed in the desktop app")
                box.operator("bmp.resync_scene", text="Resync scene to everyone", icon="FILE_REFRESH")
            if settings.scene_mismatch:
                warn = box.box()
                warn.alert = True
                warn.label(
                    text=f"Scene differs: {settings.scene_mismatch[:80]}",
                    icon="ERROR",
                )
                if settings.is_host:
                    warn.operator("bmp.resync_scene", text="Resync", icon="FILE_REFRESH")
            if settings.verify_code:
                box.label(text=f"Verify: {settings.verify_code}", icon="LOCKED")
            if settings.code:
                box.operator("bmp.copy_invite", text="Copy invite link", icon="URL")
            if settings.internet_mode:
                box.label(
                    text="Internet mode: reduced presence/interest updates",
                    icon="WORLD",
                )
            elif settings.lan_fast:
                box.label(
                    text="LAN fast path: full transform/presence rate",
                    icon="SORTTIME",
                )
            row = box.row(align=True)
            row.prop(settings, "edit_locks")
            row.operator("bmp.release_locks", text="Release locks", icon="UNLOCKED")
            if settings.lease_info:
                box.label(text=settings.lease_info[:80], icon="LOCKED")

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

        ai_box = layout.box()
        ai_box.label(text="AI Copilot", icon="LIGHT")
        ai_box.prop(settings, "ai_enabled")
        if settings.ai_enabled:
            ai_box.prop(settings, "ai_url")
            row = ai_box.row(align=True)
            row.prop(settings, "ai_model", text="")
            row.operator("bmp.ai_test", text="Test", icon="PLUGIN")
            ai_box.prop(settings, "ai_timeout")
            row = ai_box.row(align=True)
            row.prop(settings, "ai_prompt", text="")
            row.operator("bmp.ai_ask", text="", icon="PLAY")
            if settings.ai_status == "thinking":
                ai_box.label(text="Thinking...", icon="SORTTIME")
            elif settings.ai_reply:
                ai_box.label(text=settings.ai_reply[:160], icon="INFO")
            if settings.ai_pending_count:
                ai_box.label(
                    text=f"{settings.ai_pending_count} proposed edit(s)",
                    icon="CHECKMARK",
                )
                for line in settings.ai_pending_text.splitlines()[:8]:
                    ai_box.label(text=line[:100])
                row = ai_box.row(align=True)
                row.operator("bmp.ai_apply_all", text="Apply all", icon="CHECKMARK")
                row.operator("bmp.ai_clear", text="Clear", icon="X")
            if settings.last_error_code == "AI_ERROR" and settings.last_error:
                ai_box.label(text=settings.last_error[:120], icon="ERROR")

        recording_box = layout.box()
        recording_box.label(text="Recording & Replay", icon="RECORD")
        row = recording_box.row(align=True)
        if settings.recording_active:
            row.operator("bmp.record_stop", text="Stop recording", icon="PAUSE")
        else:
            row.operator("bmp.record_start", text="Record", icon="PLAY")
        row.operator("bmp.replay", text="Replay latest", icon="PREVIEW_RANGE")
        row.operator("bmp.replay_stop", text="", icon="CANCEL")
        recording_box.prop(settings, "replay_speed")
        if settings.recording_info:
            recording_box.label(text=settings.recording_info[:80], icon="FILE_TEXT")

        broadcast_box = layout.box()
        broadcast_box.label(text="Live Broadcast", icon="WORLD")
        row = broadcast_box.row(align=True)
        if settings.broadcast_active:
            row.operator("bmp.broadcast_stop", text="Stop", icon="PAUSE")
            row.operator("bmp.broadcast_open", text="Open", icon="URL")
        else:
            row.operator("bmp.broadcast_start", text="Start live view", icon="PLAY")
        broadcast_box.prop(settings, "broadcast_port")
        if settings.broadcast_url:
            broadcast_box.label(text=settings.broadcast_url[:120], icon="LINKED")

        bridge_box = layout.box()
        bridge_box.label(text="Cross-engine Bridge", icon="ARROW_LEFTRIGHT")
        row = bridge_box.row(align=True)
        row.prop(settings, "bridge_engine", text="")
        if settings.bridge_active:
            row.operator("bmp.bridge_stop", text="Stop", icon="PAUSE")
        else:
            row.operator("bmp.bridge_start", text="Start", icon="PLAY")
        bridge_box.prop(settings, "bridge_port")
        bridge_box.prop(settings, "bridge_meshes")
        if settings.bridge_active:
            bridge_box.label(
                text=f"{settings.bridge_url}  ({settings.bridge_clients} client(s))",
                icon="LINKED",
            )
            bridge_box.label(text="Receivers in integrations/", icon="INFO")

        farm_box = layout.box()
        farm_box.label(text="LAN Render Farm", icon="RENDER_STILL")
        if not settings.farm_active:
            farm_box.prop(settings, "farm_mode")
            if settings.farm_mode == "host":
                farm_box.prop(settings, "farm_port")
                farm_box.prop(settings, "farm_output")
                farm_box.prop(settings, "farm_host_worker")
                farm_box.operator(
                    "bmp.farm_start", text="Start farm host", icon="PLAY"
                )
            else:
                farm_box.prop(settings, "farm_client_url")
                farm_box.prop(settings, "farm_client_token")
                farm_box.operator("bmp.farm_join", text="Join farm", icon="PLAY")
        else:
            farm_box.label(
                text=f"{settings.farm_role.title()} · {settings.farm_url}",
                icon="LINKED",
            )
            farm_box.label(text=settings.farm_progress or "waiting", icon="TIME")
            farm_box.label(
                text=f"{settings.farm_workers} worker(s)", icon="COMMUNITY"
            )
            if settings.farm_role == "host":
                farm_box.prop(settings, "farm_use_scene_range")
                if not settings.farm_use_scene_range:
                    row = farm_box.row(align=True)
                    row.prop(settings, "farm_frame_start")
                    row.prop(settings, "farm_frame_end")
                    row.prop(settings, "farm_frame_step")
                row = farm_box.row(align=True)
                row.operator("bmp.farm_render", text="Render range", icon="RENDER_STILL")
                row.operator("bmp.farm_open", text="", icon="URL")
                row.operator("bmp.farm_copy", text="", icon="COPYDOWN")
            else:
                farm_box.operator("bmp.farm_copy", text="Copy invite", icon="COPYDOWN")
            farm_box.operator("bmp.farm_stop", text="Stop / leave", icon="PAUSE")
        farm_box.label(
            text="LAN only. Rendering blocks Blender while a frame runs.",
            icon="INFO",
        )
        farm_box.label(
            text="Frame rendering is distributed; fluid caches stay local.",
            icon="INFO",
        )

        try:
            from . import runtime as _runtime

            budgets = _runtime["perf"].snapshot().get("budgets", {})
        except Exception:  # noqa: BLE001
            budgets = {}
        if budgets:
            perf_box = layout.box()
            perf_box.label(text="Performance (p95)", icon="TIME")
            for key, label in (
                ("tick", "Tick"),
                ("apply", "Apply"),
                ("topology_read", "Topology"),
                ("bridge_pump", "Bridge"),
            ):
                item = budgets.get(key)
                if item:
                    perf_box.label(text=f"{label}: {item.get('p95_ms', 0.0):.1f} ms")

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
        box.prop(settings, "show_cameras")
        box.prop(settings, "share_view")
        box.prop(settings, "show_selections")
        box.prop(settings, "show_labels")
        box.prop(settings, "show_lasers")
        box.prop(settings, "overlay_opacity")
        box.prop(settings, "overlay_scale")
        box.prop(settings, "colorblind_safe")
        timeline = layout.box()
        timeline.label(text="Timeline", icon="TIME")
        timeline.prop(settings, "follow_host")
        timeline.prop(settings, "sync_animation_values")
        if settings.sync_animation_values:
            timeline.label(text="Live pose/shape values are shared", icon="INFO")
        else:
            timeline.label(text="Everyone scrubs their own frame", icon="INFO")
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

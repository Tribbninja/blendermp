fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "local_directory",
                "discover_directories",
                "detect_blender",
                "launch_blender",
                "find_blender_window",
                "blender_running",
                "embed_blender",
                "set_embed_bounds",
                "dock_looks_black",
                "detach_blender",
                "focus_blender",
                "set_blender_visible",
                "read_status",
                "read_settings",
                "write_settings",
                "startup_flags",
                "write_session",
                "take_deep_link",
                "log_client",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}

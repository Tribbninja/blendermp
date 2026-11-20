fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(
            tauri_build::AppManifest::new().commands(&[
                "local_directory",
                "discover_directories",
                "detect_blender",
                "launch_blender",
                "find_blender_window",
                "embed_blender",
                "set_embed_bounds",
                "detach_blender",
                "focus_blender",
                "set_blender_visible",
                "read_status",
                "startup_flags",
                "write_session",
                "log_client",
            ]),
        ),
    )
    .expect("failed to run tauri-build");
}
